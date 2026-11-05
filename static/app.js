const form = document.getElementById("search");
const input = document.getElementById("doi");
const button = document.getElementById("go");
const result = document.getElementById("result");
const hint = document.getElementById("hint");

const VERSION_LABEL = {
  publishedVersion: "versión publicada",
  acceptedVersion: "manuscrito aceptado",
  submittedVersion: "preprint",
};

const BOOK_TYPES = new Set(["book", "monograph", "edited-book", "reference-book"]);

const PROGRESS = [
  "Buscando en Unpaywall, OpenAlex y PubMed Central…",
  "Revisando repositorios: CORE, Zenodo, arXiv, bioRxiv…",
  "Verificando que el PDF sea válido…",
  "Algunos repositorios tardan un poco más…",
];

function metaLine(meta) {
  const authors = meta.authors || [];
  const who = authors.length > 3 ? `${authors[0]} et al.` : authors.join(", ");
  return [who, meta.journal || meta.publisher, meta.year].filter(Boolean).join(" · ");
}

function isBook(data) {
  return data.query_kind === "isbn" || BOOK_TYPES.has(data.meta.type);
}

async function copyText(text, btn, label) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const tmp = document.createElement("textarea");
    tmp.value = text;
    document.body.append(tmp);
    tmp.select();
    document.execCommand("copy");
    tmp.remove();
  }
  btn.textContent = "¡Copiado!";
  setTimeout(() => (btn.textContent = label), 1800);
}

function render(data) {
  result.replaceChildren();
  const tpl = document.getElementById(data.available ? "tpl-found" : "tpl-closed");
  const node = tpl.content.cloneNode(true);
  node.querySelector(".card__title").textContent = data.meta.title || data.doi || data.isbn;
  node.querySelector(".card__meta").textContent = metaLine(data.meta);

  // Identificador visible: DOI (enlace oficial) o, si no hay, ISBN.
  const idLink = node.querySelector(".doi-link");
  const idCopy = node.querySelector("[data-copy-doi]");
  const id = data.doi || data.isbn;
  const label = data.doi ? "Copiar DOI" : "Copiar ISBN";
  idLink.href = data.doi ? `https://doi.org/${data.doi}` : `https://openlibrary.org/isbn/${data.isbn}`;
  idLink.textContent = data.doi ? `DOI: ${data.doi}` : `ISBN: ${data.isbn}`;
  idCopy.textContent = label;
  idCopy.addEventListener("click", () => copyText(id, idCopy, label));
  if (data.doi && data.isbn) {
    const isbnTag = document.createElement("span");
    isbnTag.className = "isbn-tag";
    isbnTag.textContent = `ISBN ${data.isbn}`;
    node.querySelector(".card__doi").append(isbnTag);
  }

  if (data.available) {
    const s = data.source;
    const bits = [`Fuente: ${s.name}`];
    if (VERSION_LABEL[s.version]) bits.push(VERSION_LABEL[s.version]);
    if (s.license) bits.push(s.license.toUpperCase());
    node.querySelector(".card__source").textContent = bits.join(" · ");
    const a = node.querySelector("[data-download]");
    a.href = data.download_url;
    a.setAttribute("download", data.filename);
  } else {
    const status = node.querySelector(".status__text");
    const hintText = node.querySelector(".card__hint");
    if (data.is_oa) {
      status.textContent = "Gratis en la web";
      hintText.textContent =
        "Este documento es de acceso libre, pero el sitio no permite la descarga automática. Ábrelo directamente:";
    } else if (isBook(data)) {
      hintText.textContent =
        "Este libro no es de acceso abierto: no encontramos ninguna edición gratuita y legal. Puedes localizarlo en bibliotecas o consultar estas opciones:";
    }
    const list = node.querySelector(".alts");
    for (const alt of data.alternatives) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = alt.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = alt.label;
      li.append(a);
      list.append(li);
    }
  }
  result.append(node);
  result.hidden = false;
}

function showError(message) {
  result.replaceChildren();
  const div = document.createElement("div");
  div.className = "error";
  div.textContent = message;
  result.append(div);
  result.hidden = false;
}

function startProgress() {
  let i = 0;
  hint.textContent = PROGRESS[0];
  hint.hidden = false;
  const timer = setInterval(() => {
    i = Math.min(i + 1, PROGRESS.length - 1);
    hint.textContent = PROGRESS[i];
  }, 4000);
  return () => {
    clearInterval(timer);
    hint.hidden = true;
  };
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q) return;
  button.disabled = true;
  button.classList.add("loading");
  result.hidden = true;
  const stopProgress = startProgress();
  try {
    const r = await fetch(`/api/resolve?q=${encodeURIComponent(q)}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Error al buscar.");
    render(data);
    history.replaceState(null, "", `?q=${encodeURIComponent(data.doi || data.isbn)}`);
  } catch (err) {
    showError(err.message || "No se pudo completar la búsqueda.");
  } finally {
    stopProgress();
    button.disabled = false;
    button.classList.remove("loading");
  }
});

// Permite compartir enlaces del tipo /?q=10.xxxx/yyyy o /?q=9780262033848 (y los antiguos ?doi=)
const params = new URLSearchParams(location.search);
const initial = params.get("q") || params.get("doi");
if (initial) {
  input.value = initial;
  form.requestSubmit();
}
