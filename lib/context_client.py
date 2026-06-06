"""Minimal client for an OPTIONAL context-store REST API (bring your own).

Stdlib-only (urllib) so the skill has zero pip dependencies. This is the optional
durable-memory adapter; it is OFF by default (see config.json -> durable_memory.enabled).
Point it at your own REST endpoint via durable_memory.base_url and supply a bearer token in
the env var named by durable_memory.token_env.

Expected endpoint contract (adjust the paths/fields to match your API):
  POST /projects          {id, name, description}
  GET  /projects/{id}/summary
  POST /sessions          {project_id, summary}
  POST /context           {project_id, category, key, value}
  POST /decisions         {project_id, decision, rationale}

All calls are wrapped by the driver in try/except, so a schema mismatch or an unreachable
endpoint never breaks a CoThink run — durable memory is best-effort.
"""
import json
import urllib.request
import urllib.error


class ContextClient:
    def __init__(self, base_url, token, timeout=20):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method, path, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode() or "{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}

    def project_summary(self, project_id):
        return self._req("GET", f"/projects/{project_id}/summary")

    def ensure_project(self, project_id, name, description=""):
        try:
            return self.project_summary(project_id)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return self._req("POST", "/projects",
                             {"id": project_id, "name": name, "description": description})

    def log_session(self, project_id, summary):
        return self._req("POST", "/sessions", {"project_id": project_id, "summary": summary})

    def save_context(self, project_id, category, key, value):
        return self._req("POST", "/context",
                         {"project_id": project_id, "category": category,
                          "key": key, "value": value})

    def record_decision(self, project_id, decision, rationale=""):
        return self._req("POST", "/decisions",
                         {"project_id": project_id, "decision": decision,
                          "rationale": rationale})
