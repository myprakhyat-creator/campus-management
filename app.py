"""Unauthenticated event-registration API backed by Supabase.

Authentication is deliberately not implemented yet. Client identity for the
two user-scoped endpoints is supplied as ``X-User-Id`` (or ``user_id`` while
developing), and must be replaced by verified identity before production use.
"""

import os
from uuid import UUID

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from postgrest import APIError
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)


def get_supabase() -> Client:
    """Create the client only when a request needs the database."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be configured")
    return create_client(url, key)


def error(message: str, status: int):
    return jsonify({"error": message}), status


def valid_uuid(value, field_name: str):
    if not isinstance(value, str):
        return None, error(f"{field_name} must be a UUID string", 400)
    try:
        return str(UUID(value)), None
    except ValueError:
        return None, error(f"{field_name} must be a valid UUID", 400)


def current_user_id(payload=None):
    """Development-only identity lookup; this is not authentication."""
    candidate = request.headers.get("X-User-Id") or request.args.get("user_id")
    if not candidate and payload:
        candidate = payload.get("user_id")
    if not candidate:
        return None, error("user_id is required (X-User-Id header, query parameter, or JSON body)", 400)
    return valid_uuid(candidate, "user_id")


def database_error(exc: Exception):
    app.logger.exception("Supabase request failed")
    return error(getattr(exc, "message", "Database request failed"), 502)


@app.get("/")
def index():
    return jsonify({"service": "event-registration-api", "docs": "/api"})


@app.get("/healthz")
def health_check():
    """Render health check that does not depend on the external database."""
    return jsonify({"status": "ok"})


@app.get("/api")
def api_index():
    return jsonify(
        {
            "authentication": "Not implemented. Supply X-User-Id or user_id for user-scoped calls.",
            "routes": [
                "GET /api/events",
                "GET /api/events/<id>",
                "POST /api/registrations",
                "GET /api/me/registrations",
                "GET /api/events/<id>/registrations",
            ],
        }
    )


@app.get("/api/events")
def list_events():
    try:
        query = get_supabase().table("events").select("*")
        status = request.args.get("status")
        if status:
            query = query.eq("status", status)
        response = query.order("title").execute()
        return jsonify({"data": response.data})
    except APIError as exc:
        return database_error(exc)
    except Exception as exc:
        return database_error(exc)


@app.get("/api/events/<event_id>")
def get_event(event_id):
    event_id, response = valid_uuid(event_id, "event_id")
    if response:
        return response
    try:
        result = get_supabase().table("events").select("*").eq("id", event_id).limit(1).execute()
        if not result.data:
            return error("Event not found", 404)
        return jsonify({"data": result.data[0]})
    except APIError as exc:
        return database_error(exc)
    except Exception as exc:
        return database_error(exc)


@app.post("/api/registrations")
def create_registration():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("Request body must be a JSON object", 400)

    user_id, response = current_user_id(payload)
    if response:
        return response
    event_id, response = valid_uuid(payload.get("event_id"), "event_id")
    if response:
        return response

    try:
        client = get_supabase()
        event_result = client.table("events").select("id,capacity,status").eq("id", event_id).limit(1).execute()
        if not event_result.data:
            return error("Event not found", 404)
        event = event_result.data[0]
        if str(event.get("status", "")).lower() in {"cancelled", "closed", "draft"}:
            return error("Registration is not open for this event", 409)

        duplicate = (
            client.table("registrations")
            .select("id")
            .eq("event_id", event_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if duplicate.data:
            return error("User is already registered for this event", 409)

        registrations = client.table("registrations").select("id", count="exact").eq("event_id", event_id).execute()
        if event.get("capacity") is not None and (registrations.count or 0) >= event["capacity"]:
            return error("Event is at capacity", 409)

        created = client.table("registrations").insert({"event_id": event_id, "user_id": user_id}).execute()
        return jsonify({"data": created.data[0]}), 201
    except APIError as exc:
        return database_error(exc)
    except Exception as exc:
        return database_error(exc)


@app.get("/api/me/registrations")
def my_registrations():
    user_id, response = current_user_id()
    if response:
        return response
    try:
        result = (
            get_supabase()
            .table("registrations")
            .select("id,user_id,event_id,created_at,event:events(*)")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return jsonify({"data": result.data})
    except APIError as exc:
        return database_error(exc)
    except Exception as exc:
        return database_error(exc)


@app.get("/api/events/<event_id>/registrations")
def event_registrations(event_id):
    event_id, response = valid_uuid(event_id, "event_id")
    if response:
        return response
    try:
        client = get_supabase()
        event = client.table("events").select("id").eq("id", event_id).limit(1).execute()
        if not event.data:
            return error("Event not found", 404)
        result = (
            client.table("registrations")
            .select("id,user_id,event_id,created_at,user:users(id,email,role)")
            .eq("event_id", event_id)
            .order("created_at")
            .execute()
        )
        return jsonify({"data": result.data})
    except APIError as exc:
        return database_error(exc)
    except Exception as exc:
        return database_error(exc)


if __name__ == "__main__":
    app.run(debug=True)
