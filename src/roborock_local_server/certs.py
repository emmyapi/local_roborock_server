"""Certificate provisioning and renewal helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import subprocess
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import AppConfig, AppPaths


LOG = logging.getLogger("roborock_local_server.certs")
ACME_SH_PATH = Path("/opt/acme.sh/acme.sh")

# Hostnames covered by the self-signed cert presented to the vacuum on the
# iot-coordinator TLS SNI. Includes the exact hostname observed in Phase 2
# (firmware template "%siot.roborock.com" with %s = our region substring
# "roborock.mkb.dk") plus the canonical regional iot hosts, so re-onboarding
# with a different region won't require a cert reissue.
IOT_COORDINATOR_HOSTS: tuple[str, ...] = (
    "roborock.mkb.dkiot.roborock.com",
    "usiot.roborock.com",
    "euiot.roborock.com",
    "cniot.roborock.com",
    "ruiot.roborock.com",
)
IOT_COORDINATOR_CERT_DAYS = 3650


@dataclass(frozen=True)
class CertificatePaths:
    cert_file: Path
    key_file: Path


class CertificateManager:
    """Owns certificate provisioning for the release stack."""

    def __init__(self, *, config: AppConfig, paths: AppPaths) -> None:
        self.config = config
        self.paths = paths

    @property
    def certificate_paths(self) -> CertificatePaths:
        return CertificatePaths(cert_file=self.paths.cert_file, key_file=self.paths.key_file)

    @property
    def iot_coordinator_certificate_paths(self) -> CertificatePaths:
        return CertificatePaths(
            cert_file=self.paths.certs_dir / "iot_coordinator.crt",
            key_file=self.paths.certs_dir / "iot_coordinator.key",
        )

    def ensure_iot_coordinator_certificate(self) -> bool:
        """Generate a self-signed cert for *iot.roborock.com SNI responses.

        Returns True when a new cert was written, False when the existing one
        is still valid for our SAN list.
        """
        paths = self.iot_coordinator_certificate_paths
        if self._iot_cert_is_current(paths):
            return False
        self.paths.certs_dir.mkdir(parents=True, exist_ok=True)
        _write_self_signed_cert(
            cert_file=paths.cert_file,
            key_file=paths.key_file,
            hosts=IOT_COORDINATOR_HOSTS,
            valid_days=IOT_COORDINATOR_CERT_DAYS,
        )
        LOG.info(
            "Wrote self-signed iot-coordinator cert (%s) covering %s",
            paths.cert_file,
            ", ".join(IOT_COORDINATOR_HOSTS),
        )
        return True

    def _iot_cert_is_current(self, paths: CertificatePaths) -> bool:
        if not paths.cert_file.exists() or not paths.key_file.exists():
            return False
        try:
            cert = x509.load_pem_x509_certificate(paths.cert_file.read_bytes())
        except Exception:
            return False
        if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=30):
            return False
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return False
        present = {name.value for name in san.get_values_for_type(x509.DNSName)}
        return set(IOT_COORDINATOR_HOSTS).issubset(present)

    def ensure_certificate(self) -> bool:
        if self.config.tls.mode == "provided":
            if not self.paths.cert_file.exists():
                raise FileNotFoundError(f"Provided TLS cert not found: {self.paths.cert_file}")
            if not self.paths.key_file.exists():
                raise FileNotFoundError(f"Provided TLS key not found: {self.paths.key_file}")
            return False
        if not self._needs_refresh():
            return False
        self._provision_or_renew()
        return True

    def _needs_refresh(self) -> bool:
        if not self.paths.cert_file.exists() or not self.paths.key_file.exists():
            return True
        try:
            cert = x509.load_pem_x509_certificate(self.paths.cert_file.read_bytes())
        except Exception:
            return True
        deadline = datetime.now(timezone.utc) + timedelta(days=self.config.tls.renew_days_before)
        return cert.not_valid_after_utc <= deadline

    def _read_cloudflare_token(self) -> str:
        token = self.paths.cloudflare_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"Cloudflare token file is empty: {self.paths.cloudflare_token_file}")
        return token

    def _run_acme(self, args: Iterable[str]) -> None:
        self.paths.acme_dir.mkdir(parents=True, exist_ok=True)
        if not ACME_SH_PATH.exists():
            raise FileNotFoundError(f"acme.sh not found in image at {ACME_SH_PATH}")
        env = dict(os.environ)
        env["CF_Token"] = self._read_cloudflare_token()
        command = [
            str(ACME_SH_PATH),
            *args,
            "--home",
            str(self.paths.acme_dir),
            "--server",
            self.config.tls.acme_server,
        ]
        LOG.info("Running ACME command: %s", " ".join(command))
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            LOG.info("ACME output:\n%s", result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError(f"ACME command failed ({result.returncode}): {' '.join(command)}")

    def _provision_or_renew(self) -> None:
        self.paths.certs_dir.mkdir(parents=True, exist_ok=True)
        base_domain = self.config.tls.base_domain
        self._run_acme(["--register-account", "-m", self.config.tls.email])
        self._run_acme(
            [
                "--issue",
                "--dns",
                "dns_cf",
                "-d",
                base_domain,
                "-d",
                f"*.{base_domain}",
                "--keylength",
                "2048",
            ]
        )
        self._run_acme(
            [
                "--install-cert",
                "-d",
                base_domain,
                "--fullchain-file",
                str(self.paths.cert_file),
                "--key-file",
                str(self.paths.key_file),
            ]
        )
        if not self.paths.cert_file.exists() or not self.paths.key_file.exists():
            raise RuntimeError("ACME completed without writing certificate files")


def _write_self_signed_cert(
    *,
    cert_file: Path,
    key_file: Path,
    hosts: Iterable[str],
    valid_days: int,
) -> None:
    host_list = list(hosts)
    if not host_list:
        raise ValueError("At least one hostname is required for the self-signed cert")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, host_list[0])]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in host_list]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_file, 0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

