#!/usr/bin/env python3
import json
import logging
import signal
import ssl
import sys
import time
from pathlib import Path

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from imapclient import IMAPClient

CONFIG_PATH = Path("/etc/mailpush/config.json")
IDLE_TIMEOUT = 1740
DEBOUNCE_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mailpush")

running = True


def stop_handler(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    for section, keys in {
        "imap": ["host", "port", "user", "password"],
        "fcm": ["project_id", "service_account_path"],
        "push": ["tokens_file"],
    }.items():
        if section not in cfg:
            raise ValueError(f"config missing section '{section}'")
        for k in keys:
            if k not in cfg[section]:
                raise ValueError(f"config missing '{section}.{k}'")
    return cfg


class FCMSender:
    SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
    URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

    def __init__(self, project_id, service_account_path):
        self.project_id = project_id
        self.credentials = None
        self.token_expiry = 0
        self.sa_path = Path(service_account_path)

    def _get_token(self):
        if not self.sa_path.exists():
            raise FileNotFoundError(f"service account not found: {self.sa_path}")
        now = time.time()
        if self.credentials is None or now > self.token_expiry - 60:
            self.credentials = service_account.Credentials.from_service_account_file(
                str(self.sa_path), scopes=self.SCOPES
            )
            self.credentials.refresh(GoogleAuthRequest())
            self.token_expiry = self.credentials.expiry.timestamp()
        return self.credentials.token

    def send_to_all(self, tokens, title, body):
        if not tokens:
            log.warning("no device tokens registered, skipping push")
            return
        try:
            oauth = self._get_token()
        except Exception as exc:
            log.error("FCM auth failed: %s", exc)
            return
        url = self.URL.format(project=self.project_id)
        headers = {
            "Authorization": f"Bearer {oauth}",
            "Content-Type": "application/json",
        }
        sent = failed = 0
        for token in tokens:
            payload = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "android": {"priority": "HIGH"},
                }
            }
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=15)
                if resp.status_code == 200:
                    sent += 1
                else:
                    failed += 1
                    log.error("FCM error %s: %s", resp.status_code, resp.text[:200])
                    if resp.status_code == 404:
                        log.warning("token invalid/unregistered, consider removing")
            except Exception as exc:
                failed += 1
                log.error("FCM request failed: %s", exc)
        log.info("push sent=%d failed=%d", sent, failed)


def load_tokens(path):
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("tokens", []) if isinstance(data, dict) else data
    except Exception as exc:
        log.error("failed reading tokens file: %s", exc)
        return []


def inspect_new_mail(client):
    unread = client.search(["UNSEEN"])
    count = len(unread)
    snippet = ""
    if unread:
        latest = max(unread)
        fields = client.fetch(
            [latest], ["BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]"]
        )
        msg = fields.get(latest)
        raw = b""
        if isinstance(msg, dict):
            for value in msg.values():
                if isinstance(value, bytes):
                    raw += value
        elif isinstance(msg, bytes):
            raw = msg
        lines = [
            line.decode("utf-8", "replace").strip()
            for line in raw.splitlines()
            if line.strip()
        ]
        snippet = " ".join(lines)[:200]
    return count, snippet


def idle_wait(client, total):
    responses = ()
    waited = 0
    while running and waited < total:
        chunk = client.idle_check(timeout=30)
        if chunk:
            responses = chunk
            break
        waited += 30
    return responses


def watch(cfg):
    imap_cfg = cfg["imap"]
    sender = FCMSender(
        cfg["fcm"]["project_id"],
        cfg["fcm"]["service_account_path"],
    )
    while running:
        try:
            log.info("connecting %s@%s:%s", imap_cfg["user"], imap_cfg["host"], imap_cfg["port"])
            ctx = ssl.create_default_context()
            if imap_cfg.get("no_verify_ssl"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with IMAPClient(
                imap_cfg["host"],
                port=int(imap_cfg["port"]),
                ssl_context=ctx,
            ) as client:
                client.login(imap_cfg["user"], imap_cfg["password"])
                client.select_folder("INBOX")
                baseline = len(client.search(["UNSEEN"]))
                log.info(
                    "IDLE started for %s (%d unread)", imap_cfg["user"], baseline
                )
                while running:
                    client.idle()
                    responses = idle_wait(client, IDLE_TIMEOUT)
                    client.idle_done()
                    if not running:
                        break
                    if responses:
                        time.sleep(DEBOUNCE_SECONDS)
                        count, snippet = inspect_new_mail(client)
                        if count > baseline:
                            log.info(
                                "new mail detected: +%d (%s)",
                                count - baseline,
                                snippet,
                            )
                            title = "Email baru masuk"
                            body = snippet or f"{imap_cfg['user']} (+{count - baseline})"
                            sender.send_to_all(
                                load_tokens(cfg["push"]["tokens_file"]),
                                title,
                                body,
                            )
                        baseline = count
        except Exception as exc:
            log.error("connection error: %s", exc)
        if running:
            log.info("reconnecting in 10s...")
            time.sleep(10)


def main():
    try:
        cfg = load_config()
    except Exception as exc:
        log.critical("config error: %s", exc)
        sys.exit(1)
    log.info("mailpush starting (account: %s)", cfg["imap"]["user"])
    watch(cfg)
    log.info("mailpush stopped")


if __name__ == "__main__":
    main()
