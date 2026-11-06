"""Package authenticated HTTP services and Django operational persistence.

App registration occurs through ServiceConfig in INSTALLED_APPS; importing the package does
not launch ingestion, request handlers or a refresh worker.
"""
