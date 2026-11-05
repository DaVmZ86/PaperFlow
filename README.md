# PaperFlow

Del DOI al PDF, sin fricción. Escribes un **DOI o un ISBN** y PaperFlow busca una copia **legal de acceso abierto** del artículo o libro, comprueba que el archivo sea realmente un PDF y te deja descargarlo.

## Fuentes

| Tipo | Fuente | Qué aporta |
|---|---|---|
| Artículos | Página del editor | Lee la etiqueta `citation_pdf_url` de artículos de acceso libre (PLOS, Nature, Frontiers, MDPI…) |
| | PubMed Central | Colección abierta de NCBI, vía AWS Open Data |
| | Unpaywall | El mayor índice de copias abiertas (requiere `PAPERFLOW_EMAIL`) |
| | OpenAlex | Repositorios institucionales y libros OAPEN |
| | CORE | Agregador de repositorios universitarios |
| | Zenodo | Depósitos de autores (solo si el registro es el mismo trabajo, no si solo lo cita) |
| | bioRxiv / medRxiv | Preprints, incluido el preprint de un artículo ya publicado |
| | arXiv, Semantic Scholar | Preprints y `openAccessPdf` |
| | Crossref, Europe PMC | Metadatos, licencias y detección de acceso abierto |
| Libros | Crossref | Convierte ISBN en DOI si el libro tiene uno |
| | Open Library / Internet Archive | Ediciones de **dominio público** |
| | Google Books | Metadatos |

OAPEN y DOAB bloquean las consultas directas a su API desde algunas redes; sus libros igual aparecen a través de OpenAlex y Unpaywall.

## Cómo funciona

1. Reconoce el identificador: DOI (`10.xxxx/...`, `doi:...`, `https://doi.org/...`) o ISBN-10/13 con dígito de control válido.
2. Consulta todas las fuentes en paralelo. Si alguna indica que es de acceso libre, lee además la página del editor.
3. Ordena las copias candidatas (versión publicada, después manuscrito aceptado y al final preprint), descarga solo los primeros bytes de cada una (con un reintento si la fuente falla) y se queda con la primera que sea un PDF válido.
4. Si no hay ninguna descargable, indica que el documento no es de acceso abierto y muestra opciones legales: editor, repositorios, Open Library, bibliotecas cercanas (WorldCat), tu biblioteca (si configuras el proxy) y Google Scholar.

## Arranque

```powershell
.\run.ps1
```

Abre http://localhost:8000. Enlace directo: `http://localhost:8000/?q=10.1001/jama.2016.0287` o `?q=9780674637528`.

## Configuración (`.env`)

| Variable | Para qué sirve |
|---|---|
| `PAPERFLOW_EMAIL` | Activa Unpaywall y la cola prioritaria de OpenAlex y NCBI. |
| `PAPERFLOW_LIBRARY_PROXY` | Prefijo EZproxy de tu institución (botón "Acceso por mi biblioteca"). |
| `PAPERFLOW_CORE_KEY` | Clave gratuita de CORE para más consultas por minuto (opcional). |
| `PAPERFLOW_MAX_MB` | Tamaño máximo de PDF (200 MB por defecto). |

## Seguridad

- El servidor solo descarga URLs que él mismo obtuvo de las fuentes, nunca una URL enviada por el usuario.
- Cada petición, incluidas las redirecciones, se bloquea si apunta a una dirección privada o local (protección contra SSRF).
- Antes de servir un archivo, confirma que empieza con `%PDF-`.
- Los resultados se guardan en caché 1 hora; si no hubo PDF, solo 10 minutos, para reintentar repositorios lentos.
