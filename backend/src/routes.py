# Handles frontend-backend communication and routing for the backend service.

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

import config
from executor import ExecutionUnavailable, execute_code

api_routes = Blueprint("api_routes", __name__)


def error_response(message, http_status, status="invalid_request"):
    """Return a failure using the same fields as an execution result."""
    return jsonify(status=status, stdout="", stderr=message, exitCode=None,
                   executionTime=0.0), http_status


@api_routes.errorhandler(RequestEntityTooLarge)
def request_too_large(error):
    """Keep oversized request errors in the API's JSON response format."""
    return error_response("The request exceeds the size limit.", 413)


@api_routes.get("/health")
def health():
    """Report application liveness without requiring a working Docker Engine."""
    return jsonify(status="ok")


@api_routes.get("/languages")
def languages():
    """Return public language identifiers and names without container settings."""
    return jsonify(languages=[{"id": key, "name": value["name"]}
                              for key, value in config.SUPPORTED_LANGUAGES.items()])


@api_routes.post("/execute")
def execute():
    """Validate a submission and return its isolated execution result."""
    if not request.is_json:
        return error_response("Content-Type must be application/json.", 415)
    try:
        submission = request.get_json()
    except BadRequest:
        return error_response("The request contains invalid JSON.", 400)
    if not isinstance(submission, dict):
        return error_response("The request must be a JSON object.", 400)
    if set(submission) != {"language", "code"}:
        return error_response("Provide only language and code fields.", 400)
    language, code = submission["language"], submission["code"]
    if not isinstance(language, str) or language not in config.SUPPORTED_LANGUAGES:
        return error_response("Unsupported programming language.", 400)
    if not isinstance(code, str) or not code.strip() or "\x00" in code:
        return error_response("Code must be nonempty text without null characters.", 400)
    try:
        source_size = len(code.encode("utf-8"))
    except UnicodeEncodeError:
        return error_response("Code must be valid UTF-8 text.", 400)
    if source_size > config.MAX_SOURCE_SIZE:
        return error_response("Source code exceeds the size limit.", 413)
    try:
        return jsonify(execute_code(language, code))
    except ExecutionUnavailable as error:
        return error_response(str(error), 503, "unavailable")
    except Exception:
        current_app.logger.exception("Unexpected execution failure")
        return error_response("An internal execution error occurred.", 500, "internal_error")
