# Spec — Keputusan privasi, kebijakan, dan audit eksternal

Status: waiting-external
Sumber: release gate "Erasure scope", "Retention of auto-retrain exports (I-9)",
"Live feedback bound to served responses (S-5)", "Promo holdout (ADR-0010)",
"External penetration test; PCI DSS / UU PDP scoping (SEC-003)"

## Tujuan
Setiap keputusan yang bukan wewenang tim teknis diambil oleh pihak yang berwenang, lalu
diterapkan di sistem.

## Keputusan yang dibutuhkan
1. D1 — Cakupan erasure (UU PDP): apakah retensi Kafka, file dataset di disk (disaring saat
   dibaca), export auto-retrain, artefak MLflow model lama, dan baris audit (hanya id
   pseudonim) boleh tetap ada sampai masa retensinya habis.
2. D2 — Kebijakan feedback (S-5): klik yang dilaporkan customer tidak bisa dibuktikan asli.
   Seberapa besar feedback boleh memengaruhi training (retrain otomatis, bandit), dan apakah
   batas 120 panggilan per menit per customer cukup.
3. D3 — Promo holdout (ADR-0010): ukuran, durasi, dan posisi perlindungan konsumen sebelum
   `PROMO_HOLDOUT_PERCENT` > 0.
4. D4 — Pentest eksternal dan scoping PCI DSS / UU PDP.

## Pekerjaan teknis yang mungkin muncul
- Dari D1: purge dataset/export di disk, retrain untuk menghapus dari artefak MLflow,
  retensi topic Kafka yang lebih pendek.
- Dari D2: sampling atau pembobotan feedback, batas per customer yang lebih ketat, deteksi anomali.
- Dari D3: tidak ada kode baru; hanya konfigurasi dan pemantauan alert SRM yang sudah ada.
- Dari D4: perbaikan temuan pentest.

## Rujukan
Runbook `erasure`, `promo-holdout`, `learning-switches`; ADR-0007, ADR-0010, ADR-0011;
`docs/threat-model.md`.
