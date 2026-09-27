# 02 — Build, scan, sign, dan push image di CI

Status: blocked
Type: task
Owner: tim aplikasi
Blocked by: 01

## Yang dikerjakan
Job CI baru: build image `rec-engine` dan `dashboard`, scan, sign, lampirkan SBOM, push.

## Kriteria selesai
Job hijau di `main`; image dengan kerentanan kritis yang bisa diperbaiki membuat job gagal.
