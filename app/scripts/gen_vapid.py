"""Print a fresh VAPID key pair as .env lines: python -m app.scripts.gen_vapid"""
import base64

from py_vapid import Vapid

from app.services.push import public_key_b64


def main() -> None:
    vapid = Vapid()
    vapid.generate_keys()
    private_value = vapid.private_key.private_numbers().private_value
    private_key = base64.urlsafe_b64encode(private_value.to_bytes(32, "big")).rstrip(b"=").decode()
    print(f"VAPID_PUBLIC_KEY={public_key_b64(vapid)}")
    print(f"VAPID_PRIVATE_KEY={private_key}")
    print("# Optional; defaults to the first https origin in ALLOWED_ORIGINS.")
    print("# VAPID_SUBJECT=https://example.com")


if __name__ == "__main__":
    main()
