# Docker container management and code execution.

import logging
import threading
import time
import uuid

import docker
from requests.exceptions import RequestException

import config

logger = logging.getLogger(__name__)
execution_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_EXECUTIONS)


class ExecutionUnavailable(Exception):
    """Indicate capacity exhaustion or unavailable execution infrastructure."""


def collect_output(stream, stdout, stderr, output_limit, finished, failures):
    """Drain multiplexed output into bounded buffers and signal limit violations."""
    try:
        for out_chunk, err_chunk in stream:
            for chunk, target in ((out_chunk, stdout), (err_chunk, stderr)):
                if chunk:
                    remaining = config.MAX_OUTPUT_SIZE - len(stdout) - len(stderr)
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        output_limit.set()
                        return
    except Exception as error:
        failures.append(error)
    finally:
        finished.set()


def enforce_deadline(client, container_name, deadline, cancelled, timed_out):
    """Request termination at the deadline independently of Docker inspection."""
    if cancelled.wait(max(0.0, deadline - time.monotonic())):
        return
    timed_out.set()
    try:
        client.api.kill(container_name)
    except docker.errors.NotFound:
        pass  # The request thread already removed the container.
    except Exception:
        # The request thread still attempts forced removal. Only confirmed
        # removal permits its capacity reservation to be released.
        logger.exception("Deadline termination failed for container %s", container_name)


class _ExecutionState:
    """Own one submission's resources and lifecycle flags, including partial setup."""

    def __init__(self):
        """Initialize independent buffers, events and empty resource references."""
        self.client = self.container = self.stream = self.reader = None
        self.deadline_client = self.watchdog = None
        self.container_name = None
        self.reader_started = self.watchdog_started = False
        self.cancelled, self.timed_out = threading.Event(), threading.Event()
        self.output_limit, self.finished = threading.Event(), threading.Event()
        self.stdout, self.stderr = bytearray(), bytearray()
        self.failures = []
        self.started = self.deadline = None
        self.completed = False
        self.cleanup_failed = False
        self.worker_cleanup_failed = False


def create_container(state, language, code):
    """Record a Docker client and create a container with the fixed security policy."""
    runtime = config.SUPPORTED_LANGUAGES[language]
    state.client = docker.from_env(timeout=config.DOCKER_API_TIMEOUT)
    image = state.client.images.get(runtime["image"])
    if image.attrs.get("Config", {}).get("Volumes"):
        raise ExecutionUnavailable("The runtime image must not declare volumes.")
    # Record the name before creation: the daemon may succeed even if the SDK
    # fails to return a container object.
    state.container_name = "code-execution-" + uuid.uuid4().hex
    state.container = state.client.containers.create(
        image.id, entrypoint=runtime["command"], command=[code],
        name=state.container_name,
        working_dir="/tmp", user="65534:65534",
        network_disabled=True, network_mode="none",
        nano_cpus=int(config.CPU_LIMIT * 1_000_000_000),
        mem_limit=config.MEMORY_LIMIT, memswap_limit=config.MEMORY_LIMIT,
        pids_limit=config.PROCESS_LIMIT,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        privileged=False, read_only=True,
        tmpfs={"/tmp": f"rw,noexec,nosuid,nodev,size={config.TEMP_STORAGE_SIZE},mode=1777"},
        shm_size=config.SHARED_MEMORY_SIZE, stdin_open=False, tty=False,
        healthcheck={"test": ["NONE"]}, restart_policy={"Name": "no"},
        log_config=docker.types.LogConfig(type="none"), use_config_proxy=False,
    )


def start_container(state):
    """Attach output, start tracked workers, and start the container under a deadline."""
    # Attach before start to capture short programs without storing Docker logs.
    state.stream = state.container.attach(stream=True, demux=True, stdout=True,
                                          stderr=True, logs=False)
    # The watchdog must not share the polling client's HTTP session or timeout.
    state.deadline_client = docker.from_env(timeout=config.DOCKER_API_TIMEOUT)
    state.reader = threading.Thread(
        target=collect_output,
        args=(state.stream, state.stdout, state.stderr, state.output_limit,
              state.finished, state.failures), daemon=True,
    )
    try:
        state.reader.start()
        state.reader_started = True
    except RuntimeError as error:
        raise ExecutionUnavailable("Unable to start execution output reader.") from error
    state.started = time.monotonic()
    state.deadline = state.started + config.EXECUTION_TIMEOUT
    state.watchdog = threading.Thread(
        target=enforce_deadline,
        args=(state.deadline_client, state.container_name, state.deadline,
              state.cancelled, state.timed_out), daemon=True,
    )
    try:
        state.watchdog.start()
        state.watchdog_started = True
    except RuntimeError as error:
        raise ExecutionUnavailable("Unable to start execution deadline monitor.") from error
    state.client.api.timeout = max(
        0.001, min(config.DOCKER_API_TIMEOUT, state.deadline - time.monotonic())
    )
    state.container.start()


def monitor_execution(state):
    """Return outcome, exit code and elapsed seconds after observing execution."""
    status, exit_code = "success", None
    while True:
        if state.output_limit.is_set():
            status = "output_limit"
            break
        if state.failures:
            raise ExecutionUnavailable("Unable to capture execution output.")
        remaining = state.deadline - time.monotonic()
        if state.timed_out.is_set() or remaining <= 0:
            status = "timeout"
            break
        state.client.api.timeout = max(0.001, min(config.DOCKER_API_TIMEOUT, remaining))
        state.container.reload()
        # Inspection can consume the remaining budget; never accept late success.
        if state.timed_out.is_set() or time.monotonic() >= state.deadline:
            status = "timeout"
            break
        container_state = state.container.attrs["State"]
        if container_state["Status"] in {"exited", "dead"} and state.finished.is_set():
            if state.output_limit.is_set() or state.failures:
                continue
            exit_code = container_state.get("ExitCode")
            status = "success" if exit_code == 0 else "runtime_error"
            break
        state.timed_out.wait(min(0.02, max(0.0, state.deadline - time.monotonic())))
    return status, exit_code, time.monotonic() - state.started


def cleanup_execution(state):
    """Remove the container and close resources, including after partial startup.

    Return whether container removal and worker termination were confirmed.
    Preserve the normal removal attempt and fallback by name on any failure.
    Never acquire or release capacity here; record uncertain removal for the
    coordinator even if another cleanup operation raises.
    """
    state.cancelled.set()
    try:
        if state.completed:
            state.client.api.timeout = config.DOCKER_API_TIMEOUT
            state.container.remove(force=True, v=True)
            state.container = None
            state.container_name = None
            state.stream.close()
            state.reader.join(timeout=config.DOCKER_API_TIMEOUT)
            if state.reader.is_alive():
                raise ExecutionUnavailable("Unable to finish capturing execution output.")
    finally:
        if state.container_name is not None:
            try:
                state.client.api.timeout = config.DOCKER_API_TIMEOUT
                state.client.api.remove_container(state.container_name, force=True, v=True)
                state.container = None
                state.container_name = None
            except docker.errors.NotFound:
                state.container = None
                state.container_name = None
            except Exception:
                state.cleanup_failed = True
                logger.exception("Unable to remove execution container %s", state.container_name)
        if state.stream is not None:
            try:
                state.stream.close()
            except Exception:
                logger.exception("Unable to close execution output stream")
        for worker, worker_started in ((state.reader, state.reader_started),
                                       (state.watchdog, state.watchdog_started)):
            if worker_started:
                try:
                    worker.join(timeout=config.DOCKER_API_TIMEOUT)
                except Exception:
                    logger.exception("Unable to join execution worker")
                # A timed join does not guarantee termination, including when
                # closing the output stream failed. Keep capacity reserved if
                # either worker may still be running or mutating output.
                try:
                    if worker.is_alive():
                        state.worker_cleanup_failed = True
                        logger.error("Execution worker did not terminate")
                except Exception:
                    state.worker_cleanup_failed = True
                    logger.exception("Unable to confirm execution worker termination")
        for connection in (state.client, state.deadline_client):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    logger.exception("Unable to close Docker client")
    return not (state.cleanup_failed or state.worker_cleanup_failed)


def execute_code(language, code):
    """Coordinate isolated execution and own the process-local capacity reservation.

    The timeout includes start and output draining, excluding setup and cleanup.
    Actual deadline termination depends on Docker Engine responding. Failed
    removal or worker termination retains capacity: remove orphaned containers
    and restart the backend to restore it. Submitted source is never evaluated
    by a shell.
    """
    state = _ExecutionState()
    if not execution_slots.acquire(blocking=False):
        raise ExecutionUnavailable("Execution capacity is full. Try again shortly.")
    try:
        try:
            create_container(state, language, code)
            start_container(state)
            status, exit_code, duration = monitor_execution(state)
            state.completed = True
        except docker.errors.ImageNotFound as error:
            raise ExecutionUnavailable("The configured runtime image is not installed.") from error
        except (docker.errors.DockerException, RequestException, OSError) as error:
            # Only execution-stage errors participate in deadline classification.
            # Capture their time before cleanup can delay the response.
            observed_at = time.monotonic()
            if state.timed_out.is_set() or (
                state.watchdog_started and observed_at >= state.deadline
            ):
                status, exit_code = "timeout", None
                duration = observed_at - state.started
            else:
                logger.exception("Docker execution failed")
                raise ExecutionUnavailable("The execution service is unavailable.") from error
    finally:
        try:
            cleanup_execution(state)
        except (docker.errors.DockerException, RequestException, OSError) as error:
            logger.exception("Execution cleanup failed")
            raise ExecutionUnavailable("Unable to finish execution cleanup.") from error
        finally:
            if not (state.cleanup_failed or state.worker_cleanup_failed):
                execution_slots.release()
            else:
                raise ExecutionUnavailable(
                    "Execution cleanup failed; capacity is reserved until administrator recovery."
                )
    return {
        "status": status,
        "stdout": state.stdout.decode("utf-8", errors="replace"),
        "stderr": state.stderr.decode("utf-8", errors="replace"),
        "exitCode": exit_code,
        "executionTime": round(duration, 6),
    }
