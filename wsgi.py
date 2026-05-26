"""WSGI entry for Gunicorn / systemd. Do not rename `application`."""

from app import create_app

application = create_app()
