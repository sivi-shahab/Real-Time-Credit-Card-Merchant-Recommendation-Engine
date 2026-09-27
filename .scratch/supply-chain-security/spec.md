# Spec — Keamanan rantai pasok: signing dan scanning image

Status: waiting-external
Sumber: release gate "Dependency / SAST / SBOM in CI" (PARTIAL)

## Tujuan
Hanya image yang dibangun CI, dipindai, dan ditandatangani yang boleh berjalan di produksi.

## Kondisi sekarang
CI sudah menjalankan bandit, pip-audit, `npm audit`, dan membuat SBOM CycloneDX. Belum ada
registry, jadi image belum dipindai maupun ditandatangani.

## Requirement
1. R1 — CI membangun image aplikasi dan dashboard lalu mendorongnya ke registry bank.
2. R2 — Image dipindai (misalnya Trivy); build gagal untuk kerentanan kritis yang bisa diperbaiki.
3. R3 — Image ditandatangani (misalnya cosign) dan SBOM dilampirkan sebagai attestation.
4. R4 — Cluster hanya menerima image bertanda tangan (admission policy).

## Input yang ditunggu (tim platform)
Registry, identitas penandatangan (keyless OIDC atau kunci), kebijakan admission.
