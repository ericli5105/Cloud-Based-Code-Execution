# App initialization and entry point for the backend service.
from flask import Flask
from flask_cors import CORS

import config
from routes import api_routes

# Tasks: Needs to initialize Flask, set up and load configs, register routes, and start the server

def initialize_app():
    """Initializes the Flask app, loads configurations, and registers routes."""
    app = Flask(__name__)
    app.config.from_object(config)
    CORS(app, origins=[config.FRONTEND_ORIGIN])  # Allow CORS for the frontend origin
    app.register_blueprint(api_routes, url_prefix="/api")  # Register API routes
    return app

def main():
    app = initialize_app()
    app.run(
        host = config.HOST,
        port = config.PORT,
        debug = config.DEBUG
    )

if __name__ == "__main__":
    main()