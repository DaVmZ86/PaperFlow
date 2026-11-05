"""PaperFlow: resuelve un DOI o ISBN, localiza una copia legal de acceso abierto y la sirve para descarga.

Fuentes consultadas (todas legales y gratuitas):
  Artículos (DOI)
  - Crossref          metadatos + enlaces del editor con licencia abierta
  - Unpaywall         mayor índice de copias OA (requiere PAPERFLOW_EMAIL)
  - OpenAlex          ubicaciones OA, repositorios institucionales, libros OAPEN
  - PubMed Central    colección abierta de NCBI (vía AWS Open Data)
  - Europe PMC        detección de acceso abierto biomédico
  - Semantic Scholar  openAccessPdf y arXiv
  - CORE              agregador de repositorios universitarios
  - Zenodo            depósitos de autores (solo coincidencias exactas del DOI)
  - bioRxiv/medRxiv   preprints, también el preprint de un artículo ya publicado
  - Página del editor etiqueta citation_pdf_url de artículos de acceso libre
  Libros (ISBN)
  - Crossref (ISBN -> DOI), Open Library / Internet Archive (dominio público), Google Books (metadatos)

Si no hay copia abierta, devuelve opciones legales: editor, repositorios, bibliotecas, solicitud al autor.
"""

from __future__ import annotations

import asyncio
import functools
import html
import ipaddress
import os
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

CONTACT_EMAIL = os.getenv("PAPERFLOW_EMAIL", "").strip()
LIBRARY_PROXY = os.getenv("PAPERFLOW_LIBRARY_PROXY", "").strip()  # p. ej. https://ezproxy.uni.edu/login?url=
CORE_KEY = os.getenv("PAPERFLOW_CORE_KEY", "").strip()  # opcional: sube el límite de consultas a CORE
MAX_PDF_BYTES = int(os.getenv("PAPERFLOW_MAX_MB", "200")) * 1024 * 1024
OK_TTL = 60 * 60  # resultados con PDF: 1 hora
FAIL_TTL = 10 * 60  # sin PDF: 10 min (un repositorio lento puede responder después)

API_UA = "PaperFlow/0.2" + (f" (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else "")
FETCH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 PaperFlow/0.2"

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
RETRY_STATUS = {429, 500, 502, 503, 504}
VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}

app = FastAPI(title="PaperFlow")


# --------------------------------------------------------------------------- identificadores


def normalize_doi(raw: str) -> str | None:
    doi = raw.strip()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    doi = doi.strip().rstrip(".")
    return doi if DOI_RE.match(doi) else None


_PREPRINT_DOI_RE = re.compile(r"^10\.1101/(\d{4}\.\d{2}\.\d{2}\.)?\d{6,}")


def is_preprint_doi(doi: str | None) -> bool:
    """DOIs de bioRxiv/medRxiv (10.1101/2020.03.22.002386 o 10.1101/123456); excluye revistas de CSHL como 10.1101/gr.*"""
    return bool(doi and _PREPRINT_DOI_RE.match(doi))


def _isbn13_ok(s: str) -> bool:
    return sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(s)) % 10 == 0


def normalize_isbn(raw: str) -> str | None:
    """Acepta ISBN-10 o ISBN-13 (con guiones o espacios) y devuelve ISBN-13 validado."""
    s = re.sub(r"^isbn(-1[03])?:?\s*", "", raw.strip(), flags=re.I)
    s = re.sub(r"[\s-]", "", s).upper()
    if re.fullmatch(r"\d{9}[\dX]", s):
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s)) % 11:
            return None
        core = "978" + s[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
        return core + str(check)
    if re.fullmatch(r"97[89]\d{10}", s) and _isbn13_ok(s):
        return s
    return None


def parse_query(raw: str) -> tuple[str, str] | None:
    if doi := normalize_doi(raw):
        return "doi", doi
    if isbn := normalize_isbn(raw):
        return "isbn", isbn
    return None


# --------------------------------------------------------------------------- red segura


class BlockedHostError(httpx.RequestError):
    pass


@functools.lru_cache(maxsize=1024)
def _host_is_public(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (ValueError, socket.gaierror):
        return False


async def _guard_request(request: httpx.Request) -> None:
    """Se ejecuta en cada petición, incluidas las redirecciones: bloquea destinos de red interna (SSRF)."""
    host = request.url.host
    if request.url.scheme not in ("http", "https") or not host or not await asyncio.to_thread(_host_is_public, host):
        raise BlockedHostError(f"Destino bloqueado: {host}", request=request)


def make_client(read_timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(read_timeout, connect=8.0),
        follow_redirects=True,
        event_hooks={"request": [_guard_request]},
        limits=httpx.Limits(max_connections=40),
    )


# --------------------------------------------------------------------------- modelo


def safe_filename(meta: dict, ident: str) -> str:
    parts = []
    if meta.get("authors"):
        parts.append(meta["authors"][0].split()[-1])
    if meta.get("year"):
        parts.append(str(meta["year"]))
    if meta.get("title"):
        parts.append(" ".join(meta["title"].split()[:8]))
    name = "_".join(parts) or ident.replace("/", "_")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\-. ]", "", name).strip().replace(" ", "_")
    return (name[:120] or "documento") + ".pdf"


@dataclass
class Candidate:
    url: str
    source: str
    version: str = ""  # publishedVersion / acceptedVersion / submittedVersion
    license: str = ""
    host: str = ""  # publisher / repository
    priority: int = 50


@dataclass
class Resolution:
    kind: str  # "doi" | "isbn"
    ident: str
    doi: str | None = None
    isbn: str | None = None
    meta: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    landing_pages: list[dict] = field(default_factory=list)
    is_oa: bool = False
    pdf: Candidate | None = None
    created: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() - self.created > (OK_TTL if self.pdf else FAIL_TTL)


_cache: dict[str, Resolution] = {}


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict | None = None, **kw) -> dict | list | None:
    hdrs = {"User-Agent": API_UA, "Accept": "application/json"} | (headers or {})
    for attempt in range(2):
        try:
            r = await client.get(url, headers=hdrs, **kw)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in RETRY_STATUS:
                return None
        except (httpx.TimeoutException, httpx.NetworkError):
            pass
        except (httpx.HTTPError, ValueError):
            return None
        if attempt == 0:
            await asyncio.sleep(1.0)
    return None


# --------------------------------------------------------------------------- fuentes: artículos


async def from_crossref(client, doi, res: Resolution):
    data = await _get_json(client, f"https://api.crossref.org/works/{quote(doi)}")
    if not data:
        return
    m = data.get("message", {})
    authors = [
        " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
        for a in m.get("author", []) or m.get("editor", [])
    ]
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        dp = (m.get(key) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            year = dp[0][0]
            break
    isbns = [re.sub(r"\D", "", i) for i in m.get("ISBN", [])]
    res.meta.update(
        {
            "title": (m.get("title") or [""])[0],
            "authors": [a for a in authors if a],
            "journal": (m.get("container-title") or [""])[0],
            "publisher": m.get("publisher", ""),
            "year": year,
            "type": m.get("type", ""),
            "url": m.get("URL") or f"https://doi.org/{doi}",
        }
    )
    if isbns and not res.isbn:
        res.isbn = isbns[0]
    licenses = " ".join(lic.get("URL", "") for lic in m.get("license", []))
    if "creativecommons.org" in licenses:
        res.is_oa = True
        for link in m.get("link", []):
            if link.get("content-type") == "application/pdf":
                res.candidates.append(Candidate(link["URL"], "Crossref (editor)", "publishedVersion", "CC", "publisher", 30))


async def from_unpaywall(client, doi, res: Resolution):
    if not CONTACT_EMAIL:
        return
    data = await _get_json(client, f"https://api.unpaywall.org/v2/{quote(doi)}", params={"email": CONTACT_EMAIL})
    if not data:
        return
    if data.get("is_oa"):
        res.is_oa = True
    for loc in data.get("oa_locations") or []:
        if loc.get("url_for_pdf"):
            res.candidates.append(
                Candidate(
                    loc["url_for_pdf"],
                    "Unpaywall",
                    loc.get("version") or "",
                    loc.get("license") or "",
                    loc.get("host_type") or "",
                    20 if loc.get("host_type") == "repository" else 25,
                )
            )
        elif loc.get("url_for_landing_page"):
            res.landing_pages.append({"url": loc["url_for_landing_page"], "source": "Unpaywall"})


async def from_openalex(client, doi, res: Resolution):
    params = {"mailto": CONTACT_EMAIL} if CONTACT_EMAIL else {}
    data = await _get_json(client, f"https://api.openalex.org/works/doi:{quote(doi)}", params=params)
    if not data:
        return
    if (data.get("open_access") or {}).get("is_oa"):
        res.is_oa = True
    for loc in data.get("locations") or []:
        if loc.get("is_oa") and loc.get("pdf_url"):
            src = ((loc.get("source") or {}).get("display_name")) or "OpenAlex"
            res.candidates.append(
                Candidate(
                    loc["pdf_url"],
                    "OpenAlex" if src == "OpenAlex" else f"OpenAlex · {src}",
                    loc.get("version") or "",
                    loc.get("license") or "",
                    "",
                    35,
                )
            )
        elif loc.get("is_oa") and loc.get("landing_page_url"):
            res.landing_pages.append({"url": loc["landing_page_url"], "source": "OpenAlex"})
    if not res.meta.get("title") and data.get("title"):
        res.meta["title"] = data["title"]
        res.meta["year"] = data.get("publication_year")
        res.meta["authors"] = [(a.get("author") or {}).get("display_name", "") for a in data.get("authorships", [])]


async def from_semantic_scholar(client, doi, res: Resolution):
    data = await _get_json(
        client,
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi)}",
        params={"fields": "title,openAccessPdf,externalIds,isOpenAccess"},
    )
    if not data:
        return
    pdf = (data.get("openAccessPdf") or {}).get("url")
    if pdf:
        res.candidates.append(Candidate(pdf, "Semantic Scholar", "", "", "", 40))
    arxiv = (data.get("externalIds") or {}).get("ArXiv")
    if arxiv:
        res.is_oa = True
        res.candidates.append(Candidate(f"https://arxiv.org/pdf/{arxiv}", "arXiv", "submittedVersion", "", "repository", 15))


async def from_europepmc(client, doi, res: Resolution):
    # Europe PMC bloquea la descarga automática de PDFs; se usa para detectar acceso abierto y como enlace.
    data = await _get_json(
        client,
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"},
    )
    if not data:
        return
    for item in (data.get("resultList") or {}).get("result", []):
        pmcid = item.get("pmcid")
        if pmcid and item.get("isOpenAccess") == "Y":
            res.is_oa = True
            res.landing_pages.append({"url": f"https://europepmc.org/article/PMC/{pmcid}", "source": "Europe PMC"})


async def from_pmc(client, doi, res: Resolution):
    """DOI -> PMCID (NCBI) -> PDF en la colección abierta de PMC publicada en AWS Open Data."""
    params = {"ids": doi, "format": "json", "tool": "paperflow"}
    if CONTACT_EMAIL:
        params["email"] = CONTACT_EMAIL
    data = await _get_json(client, "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/", params=params)
    records = (data or {}).get("records") or []
    pmcid = records[0].get("pmcid") if records else None
    if not pmcid:
        return
    res.landing_pages.append({"url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", "source": "PMC"})
    try:
        r = await client.get(
            "https://pmc-oa-opendata.s3.amazonaws.com/",
            params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
        )
    except httpx.HTTPError:
        return
    versions = [int(v) for v in re.findall(rf"<Prefix>{pmcid}\.(\d+)/</Prefix>", r.text)]
    if versions:
        v = max(versions)
        res.is_oa = True
        res.candidates.append(
            Candidate(
                f"https://pmc-oa-opendata.s3.amazonaws.com/{pmcid}.{v}/{pmcid}.{v}.pdf",
                f"PubMed Central ({pmcid})",
                # PMC también aloja preprints (DOI 10.1101 de bioRxiv/medRxiv).
                "submittedVersion" if is_preprint_doi(doi) else "publishedVersion",
                "",
                "repository",
                5,
            )
        )


async def from_core(client, doi, res: Resolution):
    headers = {"Authorization": f"Bearer {CORE_KEY}"} if CORE_KEY else None
    data = await _get_json(
        client, "https://api.core.ac.uk/v3/search/works/", headers=headers, params={"q": f'doi:"{doi}"', "limit": 5}
    )
    for item in (data or {}).get("results") or []:
        if (item.get("doi") or "").lower() == doi.lower() and item.get("downloadUrl"):
            res.candidates.append(Candidate(item["downloadUrl"], "CORE", "", "", "repository", 45))


ZENODO_SAME = {"isidenticalto", "isversionof", "isalternateidentifier", "isnewversionof", "ispreviousversionof"}


async def from_zenodo(client, doi, res: Resolution):
    target = doi.lower()
    data = await _get_json(
        client,
        "https://zenodo.org/api/records",
        params={"q": f'doi:"{doi}" OR metadata.related_identifiers.identifier:"{doi}"', "size": 10},
    )
    for hit in ((data or {}).get("hits") or {}).get("hits") or []:
        md = hit.get("metadata") or {}
        if md.get("access_right") != "open":
            continue
        # Solo depósitos que SON el mismo trabajo (no los que simplemente lo citan).
        same = (hit.get("doi") or "").lower() == target or any(
            (ri.get("identifier") or "").lower() == target and (ri.get("relation") or "").lower() in ZENODO_SAME
            for ri in md.get("related_identifiers") or []
        )
        if not same:
            continue
        for f in hit.get("files") or []:
            link = (f.get("links") or {}).get("self")
            if link and (f.get("key") or "").lower().endswith(".pdf"):
                lic = (md.get("license") or {}).get("id", "")
                res.candidates.append(Candidate(link, "Zenodo", "", lic, "repository", 40))
                break


async def from_biorxiv(client, doi, res: Resolution):
    """Preprints de bioRxiv/medRxiv, incluido el preprint de un artículo ya publicado en una revista."""
    preprint = doi if is_preprint_doi(doi) else None
    if not preprint:
        lookups = await asyncio.gather(
            *(_get_json(client, f"https://api.biorxiv.org/pubs/{s}/{doi}") for s in ("biorxiv", "medrxiv"))
        )
        for data in lookups:
            coll = (data or {}).get("collection") or []
            if coll and coll[0].get("preprint_doi"):
                preprint = coll[0]["preprint_doi"]
                break
    if not preprint:
        return
    for server in ("biorxiv", "medrxiv"):
        data = await _get_json(client, f"https://api.biorxiv.org/details/{server}/{preprint}")
        coll = (data or {}).get("collection") or []
        if not coll:
            continue
        latest = coll[-1]
        name = "medRxiv" if server == "medrxiv" else "bioRxiv"
        res.is_oa = True
        res.candidates.append(
            Candidate(
                f"https://www.{server}.org/content/{preprint}v{latest.get('version', '1')}.full.pdf",
                f"{name} (preprint)" if preprint != doi else name,
                "submittedVersion",
                latest.get("license", ""),
                "repository",
                30,
            )
        )
        if not res.meta.get("title"):
            res.meta.update(
                {
                    "title": latest.get("title", ""),
                    "authors": [a.strip() for a in (latest.get("authors") or "").split(";") if a.strip()],
                    "journal": name,
                    "year": (latest.get("date") or "")[:4] or None,
                }
            )
        return


_META_PDF_RE = re.compile(r"<meta\s[^>]*?(?:name|property)\s*=\s*[\"']citation_pdf_url[\"'][^>]*>", re.I)
_CONTENT_RE = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.I)


async def from_landing_page(client, doi, res: Resolution):
    """Lee la página del artículo en el editor y extrae la etiqueta estándar citation_pdf_url."""
    try:
        async with client.stream(
            "GET", f"https://doi.org/{quote(doi)}", headers={"User-Agent": FETCH_UA, "Accept": "text/html,*/*"}
        ) as r:
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return
            buf = b""
            async for chunk in r.aiter_bytes(65536):
                buf += chunk
                if len(buf) > 800_000 or b"</head>" in buf[-70000:].lower():
                    break
            final_url = str(r.url)
    except httpx.HTTPError:
        return
    text = buf.decode("utf-8", "ignore")
    tag = _META_PDF_RE.search(text)
    content = _CONTENT_RE.search(tag.group(0)) if tag else None
    if not content:
        return
    pdf_url = urljoin(final_url, html.unescape(content.group(1).strip()))
    host = (urlparse(final_url).hostname or "").removeprefix("www.")
    res.candidates.append(Candidate(pdf_url, f"Editor ({host})", "publishedVersion", "", "publisher", 3))


# --------------------------------------------------------------------------- fuentes: libros


async def isbn_to_doi(client, isbn: str) -> str | None:
    data = await _get_json(client, "https://api.crossref.org/works", params={"filter": f"isbn:{isbn}", "rows": 5})
    items = ((data or {}).get("message") or {}).get("items") or []
    # Preferimos el libro completo sobre un capítulo.
    items.sort(key=lambda i: 0 if i.get("type") in ("book", "monograph", "edited-book", "reference-book") else 1)
    return items[0].get("DOI") if items else None


async def from_open_library(client, isbn, res: Resolution):
    data = await _get_json(
        client,
        "https://openlibrary.org/search.json",
        params={
            "isbn": isbn,
            "fields": "key,title,author_name,first_publish_year,publisher,ebook_access,ia",
            "limit": 1,
        },
    )
    docs = (data or {}).get("docs") or []
    if not docs:
        return
    d = docs[0]
    res.meta.setdefault("title", d.get("title", ""))
    res.meta.setdefault("authors", d.get("author_name") or [])
    res.meta.setdefault("publisher", (d.get("publisher") or [""])[0])
    res.meta.setdefault("year", d.get("first_publish_year"))
    res.landing_pages.append({"url": f"https://openlibrary.org{d['key']}", "source": "Open Library"})
    # Solo obras de dominio público: el Internet Archive las ofrece en descarga libre.
    if d.get("ebook_access") == "public":
        res.is_oa = True
        for ia in (d.get("ia") or [])[:3]:
            res.candidates.append(
                Candidate(f"https://archive.org/download/{ia}/{ia}.pdf", "Internet Archive (dominio público)", "", "PD", "repository", 20)
            )


async def from_google_books(client, isbn, res: Resolution):
    data = await _get_json(client, "https://www.googleapis.com/books/v1/volumes", params={"q": f"isbn:{isbn}"})
    items = (data or {}).get("items") or []
    if not items:
        return
    v = items[0].get("volumeInfo") or {}
    res.meta.setdefault("title", " — ".join(filter(None, [v.get("title"), v.get("subtitle")])))
    res.meta.setdefault("authors", v.get("authors") or [])
    res.meta.setdefault("publisher", v.get("publisher", ""))
    res.meta.setdefault("year", (v.get("publishedDate") or "")[:4] or None)
    if v.get("infoLink"):
        res.landing_pages.append({"url": v["infoLink"], "source": "Google Books"})


# --------------------------------------------------------------------------- verificación y orquestación


async def looks_like_pdf(client: httpx.AsyncClient, cand: Candidate) -> bool:
    """Descarga solo los primeros bytes para confirmar que es un PDF real; reintenta una vez si la fuente falla."""
    for attempt in range(2):
        try:
            async with client.stream("GET", cand.url, headers={"User-Agent": FETCH_UA, "Accept": "application/pdf,*/*"}) as r:
                if r.status_code in (200, 206):
                    async for chunk in r.aiter_bytes(1024):
                        return chunk.lstrip()[:5] == b"%PDF-"
                    return False
                if r.status_code not in RETRY_STATUS:
                    return False
        except (httpx.TimeoutException, httpx.NetworkError):
            pass
        except httpx.HTTPError:
            return False
        if attempt == 0:
            await asyncio.sleep(1.5)
    return False


async def pick_pdf(res: Resolution) -> None:
    seen: set[str] = set()
    unique = []
    for c in sorted(res.candidates, key=lambda c: (VERSION_RANK.get(c.version, 1), c.priority)):
        key = c.url.split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(c)
    res.candidates = unique
    async with make_client(read_timeout=20.0) as client:
        checks = await asyncio.gather(*(looks_like_pdf(client, c) for c in unique))
    for cand, ok in zip(unique, checks):
        if ok:
            res.pdf = cand
            res.is_oa = True
            break


async def resolve_doi(doi: str) -> Resolution:
    res = Resolution(kind="doi", ident=doi, doi=doi)
    async with make_client() as client:
        await asyncio.gather(
            from_crossref(client, doi, res),
            from_unpaywall(client, doi, res),
            from_openalex(client, doi, res),
            from_semantic_scholar(client, doi, res),
            from_europepmc(client, doi, res),
            from_pmc(client, doi, res),
            from_core(client, doi, res),
            from_zenodo(client, doi, res),
            from_biorxiv(client, doi, res),
        )
        # Si alguna fuente indica que es de acceso libre, intentamos la versión del propio editor.
        if res.is_oa:
            await from_landing_page(client, doi, res)
        # Libros con DOI: buscamos también una edición de dominio público en Open Library.
        if res.isbn and res.meta.get("type") in BOOK_TYPES:
            await from_open_library(client, res.isbn, res)
    await pick_pdf(res)
    return res


async def resolve_isbn(isbn: str) -> Resolution:
    async with make_client() as client:
        doi = await isbn_to_doi(client, isbn)
    if doi:
        res = await resolve(("doi", doi))
        if not res.pdf and isbn != res.isbn:
            # El ISBN escrito puede ser otra edición (impresa/electrónica) distinta de la que registra Crossref.
            before = len(res.candidates)
            async with make_client() as client:
                await from_open_library(client, isbn, res)
            if len(res.candidates) > before:
                await pick_pdf(res)
        res.isbn = res.isbn or isbn
        return res
    res = Resolution(kind="isbn", ident=isbn, isbn=isbn)
    res.meta["type"] = "book"
    async with make_client() as client:
        await asyncio.gather(from_open_library(client, isbn, res), from_google_books(client, isbn, res))
    await pick_pdf(res)
    return res


async def resolve(query: tuple[str, str]) -> Resolution:
    key = f"{query[0]}:{query[1]}"
    cached = _cache.get(key)
    if cached and not cached.expired():
        return cached
    res = await (resolve_doi(query[1]) if query[0] == "doi" else resolve_isbn(query[1]))
    _cache[key] = res
    return res


BOOK_TYPES = {"book", "monograph", "edited-book", "reference-book", "book-chapter", "book-section", "book-part"}


def alternatives(res: Resolution) -> list[dict]:
    alts = []
    title = res.meta.get("title", "")
    if res.doi:
        alts.append({"label": "Página del editor", "url": f"https://doi.org/{res.doi}", "kind": "publisher"})
    seen_hosts: set[str] = set()
    for lp in res.landing_pages:
        host = (urlparse(lp["url"]).hostname or "").removeprefix("www.")
        if host == "ncbi.nlm.nih.gov":
            host = "pmc.ncbi.nlm.nih.gov"
        if not host or host in seen_hosts or host in ("doi.org", "dx.doi.org"):
            continue
        seen_hosts.add(host)
        label = f"Ver en {lp['source']}" if lp["source"] in ("Open Library", "Google Books") else f"Versión abierta en {host}"
        alts.append({"label": label, "url": lp["url"], "kind": "oa-landing"})
        if len(seen_hosts) == 4:
            break
    if res.isbn and (res.kind == "isbn" or res.meta.get("type") in BOOK_TYPES):
        alts.append({"label": "Buscar en bibliotecas cercanas (WorldCat)", "url": f"https://search.worldcat.org/isbn/{res.isbn}", "kind": "library"})
    if LIBRARY_PROXY and res.doi:
        alts.append(
            {"label": "Acceso por mi biblioteca", "url": LIBRARY_PROXY + quote(f"https://doi.org/{res.doi}", safe=""), "kind": "library"}
        )
    q = f'"{title}"' if title else (res.doi or res.isbn or "")
    alts.append({"label": "Buscar otras versiones en Google Scholar", "url": f"https://scholar.google.com/scholar?q={quote(q)}", "kind": "scholar"})
    return alts


# --------------------------------------------------------------------------- API


def _parse_or_400(q: str | None, doi: str | None) -> tuple[str, str]:
    parsed = parse_query(q or doi or "")
    if not parsed:
        raise HTTPException(400, "Escribe un DOI (p. ej. 10.1038/nature12373) o un ISBN de 10 o 13 dígitos.")
    return parsed


@app.get("/api/resolve")
async def api_resolve(q: str | None = Query(None, max_length=300), doi: str | None = Query(None, max_length=300)):
    query = _parse_or_400(q, doi)
    res = await resolve(query)
    if not res.meta.get("title") and not res.candidates:
        what = "DOI" if query[0] == "doi" else "ISBN"
        raise HTTPException(404, f"No se encontró ningún registro para ese {what}.")
    ident = res.doi or res.isbn
    body = {
        "query_kind": query[0],
        "doi": res.doi,
        "isbn": res.isbn,
        "meta": res.meta,
        "available": res.pdf is not None,
        "is_oa": res.is_oa,
        "source": None,
        "alternatives": alternatives(res),
        "unpaywall_enabled": bool(CONTACT_EMAIL),
    }
    if res.pdf:
        version = "submittedVersion" if is_preprint_doi(res.doi) else res.pdf.version
        body["source"] = {"name": res.pdf.source, "version": version, "license": res.pdf.license}
        body["download_url"] = f"/api/download?q={quote(f'{query[1]}', safe='')}"
        body["filename"] = safe_filename(res.meta, ident)
    return JSONResponse(body)


@app.get("/api/download")
async def api_download(q: str | None = Query(None, max_length=300), doi: str | None = Query(None, max_length=300)):
    query = _parse_or_400(q, doi)
    res = await resolve(query)
    if not res.pdf:
        raise HTTPException(404, "No hay una copia de acceso abierto disponible.")

    # Solo se descargan URLs que el propio servidor resolvió y verificó (nunca una URL enviada por el usuario).
    client = make_client(read_timeout=60.0)
    req = client.build_request("GET", res.pdf.url, headers={"User-Agent": FETCH_UA, "Accept": "application/pdf,*/*"})
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        _cache.pop(f"{query[0]}:{query[1]}", None)
        raise HTTPException(502, "La fuente no respondió. Intenta de nuevo.")
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        _cache.pop(f"{query[0]}:{query[1]}", None)
        raise HTTPException(502, "La fuente no respondió correctamente. Intenta de nuevo.")

    async def body():
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                sent += len(chunk)
                if sent > MAX_PDF_BYTES:
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    filename = safe_filename(res.meta, res.doi or res.isbn or query[1])
    return StreamingResponse(
        body(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
