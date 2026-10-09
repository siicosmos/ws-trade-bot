import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERTS = os.path.join(REPO, "certs")


def subject_alt_names():
    names = ["localhost", "ws-trade-bot", socket.gethostname()]
    ips = ["127.0.0.1"]
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.append(ip)
    except OSError:
        pass
    return names, ips


def main():
    os.makedirs(CERTS, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "ws-trade-bot")]
    )
    dns_names, ips = subject_alt_names()
    san = x509.SubjectAlternativeName(
        [x509.DNSName(n) for n in dns_names]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = os.path.join(CERTS, "dashboard.crt")
    key_path = os.path.join(CERTS, "dashboard.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    # the private key must not be world-readable (a no-op on
    # windows, where ntfs acls govern)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    print(f"certificate: {cert_path}")
    print(f"private key: {key_path}")
    print(f"valid for: {', '.join(dns_names + ips)}")
    print(
        "config.yaml:\n"
        "pipeline:\n"
        '  tls_cert: "certs/dashboard.crt"\n'
        '  tls_key: "certs/dashboard.key"'
    )
    print(
        "to remove browser warnings, double-click certs/dashboard.crt "
        "and install it into Trusted Root Certification Authorities"
    )


if __name__ == "__main__":
    main()
