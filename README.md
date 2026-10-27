# Referidos · Pulppo

Herramienta del programa de referidos: el asesor ve sus beneficios (a cobrar, en proceso, cobrados, a futuro) y sube su
factura; Finanzas y KAMs aprueban o rechazan (con motivo) y Finanzas marca como pagado.

## Variables de entorno (Vercel → Settings → Environment Variables)
| Variable | Obligatoria | Qué es |
|---|---|---|
| `SECRET_KEY` | Sí | Texto aleatorio largo; firma sesiones y enlaces de acceso |
| `MONGODB_URI` | Sí | MongoDB de Pulppo con usuario de **solo lectura** |
| `DATABASE_URL` o `POSTGRES_URL` | Sí | Postgres (Neon desde el Marketplace de Vercel): solicitudes, historial, avisos |
| `BLOB_READ_WRITE_TOKEN` | Sí | Vercel Blob: facturas PDF/XML y comprobantes |
| `BROKER_SSO_URL` | Sí | `https://broker.pulppo.com/referidos-sso`: el login es el de broker (vuelve a `/auth/sso?key=...`) |
| `PULPPO_API_URL` | Opcional | API de Pulppo donde se canjea el key (default `https://api.pulppo.com`) |
| `RESEND_API_KEY` + `CORREO_REMITENTE` | Recomendada | Correos (rechazo con motivo, aprobación, pago) |
| `CRON_SECRET` | Sí | Protege el cron diario que registra cuándo se pagó cada regalía |
| `FINANZAS_EMAILS` | Opcional | Correos de Finanzas separados por coma (se suman a config.json) |
| `RFC_PULPPO` | Opcional | RFC receptor esperado en las facturas (se valida en la revisión) |
