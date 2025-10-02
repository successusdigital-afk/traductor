# Traductor de documentos

Aplicación web construida con FastAPI para traducir documentos PDF o EPUB del inglés al español en lotes de 20, 40 o 60 páginas. Cada lote genera automáticamente un archivo de Microsoft Word y el panel principal permite seguir el progreso de la traducción.

## Requisitos

- Python 3.11+
- Dependencias del sistema necesarias para `torch` y `ebooklib` (en sistemas Debian/Ubuntu puede requerir `libxml2` y `libxslt`).

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Uso

```bash
uvicorn app.main:app --reload
```

Luego abre `http://localhost:8000` en el navegador para acceder al dashboard:

1. Carga un archivo en formato PDF o EPUB.
2. Selecciona el tamaño del lote (20, 40 o 60 páginas).
3. Pulsa **Traducir documento** para iniciar el procesamiento en segundo plano.

El dashboard actualizará automáticamente el estado de cada trabajo y podrás descargar los archivos DOCX generados por lote en la sección **Archivos generados**.

## Notas

- La traducción se realiza con el modelo `Helsinki-NLP/opus-mt-en-es` de Hugging Face. La primera ejecución descargará los pesos del modelo.
- Los documentos cargados se almacenan en `app/storage/uploads/` y los lotes generados en `app/storage/output/`.
