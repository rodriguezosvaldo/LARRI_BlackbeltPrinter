"""Consulta seqs.reply y rr_reply de ambas impresoras."""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PRINTER1_IP = os.getenv("PRINTER1_IP")
PRINTER2_IP = os.getenv("PRINTER2_IP")
TIMEOUT = 10
REQUESTS_EVERY = 1  # seconds


def get_seqs_reply(printer_ip: str) -> dict:
    r = requests.get(
        f"{printer_ip}/rr_model",
        params={"key": "seqs.reply", "flags": "d99n"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("result")


def get_rr_reply(printer_ip: str) -> str:
    r = requests.get(f"{printer_ip}/rr_reply", timeout=TIMEOUT)
    r.raise_for_status()
    return r.text.strip()


def query_printer(name: str, printer_ip: str) -> None:
    print(f"\n=== {name} ({printer_ip}) ===")
    try:
        print(f"seqs.reply: {get_seqs_reply(printer_ip)}")
    except Exception as e:
        print(f"seqs.reply: ERROR - {e}")
    try:
        reply = get_rr_reply(printer_ip)
        print(f"rr_reply: {reply!r}" if reply else "rr_reply: (vacío)")
    except Exception as e:
        print(f"rr_reply: ERROR - {e}")


def main() -> None:
    while True:
        query_printer("Printer 1", PRINTER1_IP)
        query_printer("Printer 2", PRINTER2_IP)
        time.sleep(REQUESTS_EVERY)


if __name__ == "__main__":
    main()
