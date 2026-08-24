#!/usr/bin/env python3
import json
import logging
import signal
import ssl
import sys
import threading
import time
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr
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
    if "accounts" not in cfg or not cfg["accounts"]:
        raise ValueError("config missing 'accounts' list")
    required = {"host", "port", "user", "password"}
    for i, acc in enumerate(cfg["accounts"]):
        missing = required - set(acc)
        if missing:
            raise ValueError(f"accounts[{i}] missing: {missing}")
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
                    "data": {"title": title[:200], "body": body[:200]},
                    "android": {
                        "priority": "HIGH",
                        "ttl": "3600s",
                    },
                }
            }
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=15)
                if resp.status_code == 200:
                    sent += 1
                else:
                    failed += 1
                    log.error("FCM error %s: %s", resp.status_code, resp.text[:300])
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


def decode_mime(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def fetch_newest_header(client, msns):
    fields = client.fetch(msns, ["BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]"])
    sender = subject = ""
    latest = max(fields.keys()) if fields else None
    if latest is not None:
        msg = fields[latest]
        raw = b""
        if isinstance(msg, dict):
            for value in msg.values():
                if isinstance(value, bytes):
                    raw += value
        elif isinstance(msg, bytes):
            raw = msg
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        addr_name, addr_mail = parseaddr(str(parsed.get("From", "")))
        sender = decode_mime(addr_name) or addr_mail
        subject = decode_mime(parsed.get("Subject"))
    return sender, subject


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


def watch_account(acc, sender):
    ctx = ssl.create_default_context()
    if acc.get("no_verify_ssl"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    while running:
        try:
            log.info(
                "[%s] connecting %s:%s", acc["user"], acc["host"], acc["port"]
            )
            with IMAPClient(
                acc["host"], port=int(acc["port"]), ssl_context=ctx
            ) as client:
                client.login(acc["user"], acc["password"])
                folder_info = client.select_folder("INBOX")
                next_uid = int(folder_info[b"UIDNEXT"])
                log.info("[%s] IDLE started (next UID %d)", acc["user"], next_uid)
                while running:
                    client.idle()
                    responses = idle_wait(client, IDLE_TIMEOUT)
                    client.idle_done()
                    if not running:
                        break
                    if responses:
                        time.sleep(DEBOUNCE_SECONDS)
                        folder_info = client.select_folder("INBOX")
                        cur = int(folder_info[b"UIDNEXT"])
                        if cur > next_uid:
                            new_uids = list(range(next_uid, cur))
                            delta = len(new_uids)
                            who, subject = fetch_newest_header(
                                client, new_uids[-5:]
                            )
                            title = subject or "(tanpa subjek)"
                            body = f"{who or '?'} → {acc['user']}"
                            if delta > 1:
                                body += f" (+{delta} pesan baru)"
                            log.info(
                                "[%s] new mail: +%d from=%s subj=%s",
                                acc["user"],
                                delta,
                                who,
                                subject,
                            )
                            sender.send_to_all(
                                load_tokens(sender.tokens_file), title, body
                            )
                        next_uid = max(next_uid, cur)
        except Exception as exc:
            log.error("[%s] connection error: %s", acc.get("user"), exc)
        if running:
            log.info("[%s] reconnecting in 10s...", acc.get("user"))
            time.sleep(10)


def main():
    try:
        cfg = load_config()
    except Exception as exc:
        log.critical("config error: %s", exc)
        sys.exit(1)
    fcm_sender = FCMSender(
        cfg["fcm"]["project_id"], cfg["fcm"]["service_account_path"]
    )
    fcm_sender.tokens_file = cfg["push"]["tokens_file"]
    threads = []
    for acc in cfg["accounts"]:
        t = threading.Thread(target=watch_account, args=(acc, fcm_sender), daemon=True)
        t.start()
        threads.append(t)
    log.info("mailpush watching %d account(s)", len(threads))
    while any(t.is_alive() for t in threads) and running:
        time.sleep(1)
    log.info("mailpush stopped")


if __name__ == "__main__":
    main()
