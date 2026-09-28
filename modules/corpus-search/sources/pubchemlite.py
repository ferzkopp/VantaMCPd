#!/usr/bin/env python3
"""PubChemLite source adapter: a curated, annotation-ranked subset of PubChem compounds.

PubChemLite for Exposomics is a single CSV of a few hundred thousand compounds, each carrying a
PubChem CID, an InChIKey, a molecular formula, and counts across ten PubChem annotation categories.
Those counts are a subject scheme of its own, so this is the first source whose categories
`corpus_categories` can report without borrowing arXiv's taxonomy.

Releases are published as Zenodo versions rather than through an incremental feed. The concept
record always resolves to the newest one, so acquisition reads its metadata first and learns the
download URL, size and checksum without fetching the CSV.
"""
import csv
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fetching import clean_text, download_file, emit_progress, user_agent

CONCEPT_RECORD = "https://zenodo.org/api/records/5995885"
CONCEPT_DOI = "10.5281/zenodo.5995885"
LANDING_URL = "https://doi.org/10.5281/zenodo.5995885"
COMPOUND_URL = "https://pubchem.ncbi.nlm.nih.gov/compound/"
TERMS_URL = "https://creativecommons.org/licenses/by/4.0/"
LICENSE = "CC BY 4.0"
MAX_METADATA_BYTES = 1024 * 1024
MAX_CID_DIGITS = 12
MAX_FIELD_BYTES = 4 * 1024 * 1024
BATCH_RECORDS = 2000
PROGRESS_INTERVAL_ROWS = 25_000

# The annotation columns, in the order PubChemLite declares them. A compound's primary category is
# the one it is most annotated under, so ties resolve to the earlier column here.
ANNOTATIONS = (
    ("AgroChemInfo", "Agrochemical Information"),
    ("BioPathway", "Biological Pathways"),
    ("DrugMedicInfo", "Drug and Medication Information"),
    ("FoodRelated", "Food Related"),
    ("PharmacoInfo", "Pharmacology and Biochemistry"),
    ("SafetyInfo", "Safety and Hazards"),
    ("ToxicityInfo", "Toxicity Information"),
    ("KnownUse", "Known Uses"),
    ("DisorderDisease", "Disorders and Diseases"),
    ("Identification", "Identification"),
    ("NORMANSLE", "NORMAN Suspect List Exchange"),
)
ANNOTATION_NAMES = dict(ANNOTATIONS)
SUBJECT_GROUP = "PubChemLite Annotations"
REQUIRED_COLUMNS = ("Identifier", "CompoundName", "InChIKey", "MolecularFormula")


def annotation_count(row: dict[str, Any], column: str) -> int:
    value = (row.get(column) or "").strip()
    try:
        return max(int(value), 0)
    except ValueError:
        return 0


def release_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Pick the CSV asset out of a Zenodo record, with the identity needed to detect a new release."""
    files = [item for item in payload.get("files", []) if str(item.get("key", "")).endswith(".csv")]
    if not files:
        raise ValueError("the PubChemLite Zenodo record publishes no CSV file")
    asset = max(files, key=lambda item: int(item.get("size") or 0))
    checksum = str(asset.get("checksum") or "").split(":")[-1]
    size = int(asset.get("size") or 0)
    if not checksum or size <= 0:
        raise ValueError("the PubChemLite Zenodo record names no checksum or size for its CSV")
    metadata = payload.get("metadata") or {}
    published = str(metadata.get("publication_date") or "")[:10]
    return {
        "key": str(asset["key"]),
        "url": str((asset.get("links") or {}).get("self") or ""),
        "size": size,
        "identity": f"{checksum}:{size}",
        "version": str(metadata.get("version") or ""),
        "doi": str(payload.get("doi") or CONCEPT_DOI),
        "cutoff": f"{published}T00:00:00Z" if published else "",
    }


def fetch_release(opener: Callable[..., Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        CONCEPT_RECORD, headers={"User-Agent": user_agent(), "Accept": "application/json"}
    )
    with opener(request, timeout=60) as response:
        payload = json.loads(response.read(MAX_METADATA_BYTES).decode("utf-8"))
    release = release_metadata(payload)
    if not release["url"]:
        raise ValueError("the PubChemLite Zenodo record names no download URL for its CSV")
    return release


def compound_record(
    row: dict[str, Any], release: dict[str, Any], source_query: str, fetched_at: str
) -> dict[str, Any] | None:
    cid = (row.get("Identifier") or "").strip()
    if not cid.isdigit() or len(cid) > MAX_CID_DIGITS:
        return None
    synonym = clean_text(row.get("Synonym"))
    title = clean_text(row.get("CompoundName")) or synonym or f"PubChem CID {cid}"
    formula = clean_text(row.get("MolecularFormula"))
    key = clean_text(row.get("InChIKey"))
    mass = clean_text(row.get("MonoisotopicMass"))
    logp = clean_text(row.get("XLogP"))
    counts = {column: annotation_count(row, column) for column, _ in ANNOTATIONS}
    categories = [column for column, _ in ANNOTATIONS if counts[column]]
    primary = max(categories, key=lambda column: counts[column], default=None)

    # The annotation names are deliberately absent: they are already indexed as categories, and
    # listing them here made a well-annotated compound's abstract several times longer than a sparse
    # one's, so BM25 length normalization ranked the best-documented compounds last.
    described = [
        f"Also known as {synonym}." if synonym and synonym.casefold() != title.casefold() else "",
        f"Molecular formula {formula}." if formula else "",
        f"Monoisotopic mass {mass}." if mass else "",
        f"XLogP {logp}." if logp else "",
        f"InChIKey {key}." if key else "",
    ]
    structure = [
        f"SMILES {row['SMILES'].strip()}" if clean_text(row.get("SMILES")) else "",
        clean_text(row.get("InChI")),
    ]
    return {
        "id": f"pubchem:{int(cid)}",
        "source": "pubchem",
        "license": LICENSE,
        "title": title,
        "abstract": " ".join(part for part in described if part),
        "authors_json": "[]",
        "authors_search": "",
        "categories_json": json.dumps(categories, separators=(",", ":")),
        "categories_search": "|" + "|".join(categories) + "|" if categories else "||",
        "primary_category": primary,
        "published": release["cutoff"],
        "updated": release["cutoff"],
        "doi": release["doi"],
        "journal_ref": None,
        # SMILES and InChI belong to the record but not to the index: they would multiply its size
        # without making any plain-language query more answerable.
        "comment": "; ".join(part for part in structure if part) or None,
        "abstract_url": f"{COMPOUND_URL}{int(cid)}",
        "pdf_url": None,
        "source_query": source_query,
        "profile_slice": "compound-index",
        "fetched_at": fetched_at,
    }


class PubChemLiteSource:
    """Annotation-ranked PubChem compound metadata from the PubChemLite for Exposomics release."""

    key = "pubchemlite-compound-index"
    id = "pubchem"
    prefix = "pubchem"
    name = "PubChemLite for Exposomics compound index"
    license = LICENSE
    terms_url = TERMS_URL
    catchup_source_url = None
    subject_scheme = "pubchemlite"
    supports_topics = False
    supports_catch_up = False

    @property
    def source_url(self) -> str:
        return LANDING_URL

    def validate(self, spec: dict[str, Any]) -> None:
        if spec.get("samplePercent") not in (1, 25, 100):
            raise ValueError("pubchemlite samplePercent must be 1, 25, or 100")
        if not isinstance(spec.get("sampleSeed"), str) or not spec["sampleSeed"]:
            raise ValueError("pubchemlite sampleSeed must be a non-empty string")
        if spec.get("topics"):
            raise ValueError("pubchemlite does not accept topics; its annotations describe records rather than select them")

    def identity_fields(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": self.key,
            "samplePercent": spec["samplePercent"],
            "sampleSeed": spec["sampleSeed"],
        }

    def acquire(self, data_dir: Path, spec: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        local = overrides.get("dataset_path")
        if local is not None:
            dataset = Path(local)
            stat = dataset.stat()
            release = {
                "key": dataset.name,
                "url": "",
                "size": stat.st_size,
                "identity": f"local:{stat.st_size}:{int(stat.st_mtime)}",
                "version": "local",
                "doi": CONCEPT_DOI,
                "cutoff": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        else:
            opener = overrides.get("opener", urllib.request.urlopen)
            release = fetch_release(opener)
            dataset = data_dir / "source" / release["key"]
            download_file(dataset, release["url"], "Downloading the PubChemLite compound release.", opener)
        if not release["cutoff"]:
            release["cutoff"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"identity": release["identity"], "dataset": dataset, "release": release}

    def ingest(self, connection: sqlite3.Connection, spec: dict[str, Any], handle: dict[str, Any], state: dict[str, Any], save: Callable[[], None], fetched_at: str, upsert, sampled) -> tuple[int, str]:
        release = handle["release"]
        cutoff = release["cutoff"]
        source_query = f"pubchemlite:{release['version'] or 'latest'};sample={spec['samplePercent']}%"
        # Rows are counted rather than byte-offset, because a quoted CSV field may span lines and the
        # reader cannot report a position mid-iteration. A resumed ingest re-parses and skips instead.
        skip = int(state.get("rows", 0))
        records = int(state.get("records", 0)) if skip else 0
        seen = 0
        batch: list[dict[str, Any]] = []
        csv.field_size_limit(MAX_FIELD_BYTES)
        with handle["dataset"].open("r", newline="", encoding="utf-8", errors="replace") as source:
            reader = csv.DictReader(source)
            missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"the PubChemLite release is missing expected columns: {', '.join(missing)}")
            for row in reader:
                seen += 1
                if seen <= skip:
                    continue
                cid = (row.get("Identifier") or "").strip()
                if cid.isdigit() and sampled(cid, spec["samplePercent"], spec["sampleSeed"]):
                    record = compound_record(row, release, source_query, fetched_at)
                    if record is not None:
                        batch.append(record)
                if len(batch) >= BATCH_RECORDS:
                    upsert(connection, batch)
                    records += len(batch)
                    batch.clear()
                    connection.commit()
                    state.update({"rows": seen, "records": records, "cutoff": cutoff})
                    save()
                if seen % PROGRESS_INTERVAL_ROWS == 0:
                    emit_progress("ingest", seen, 0, f"Indexed {records} PubChemLite compounds.", "compounds")
            if batch:
                upsert(connection, batch)
                records += len(batch)
                connection.commit()
        state.update({"rows": seen, "records": records, "cutoff": cutoff})
        save()
        return records, cutoff

    def catch_up(self, *arguments: Any, **keywords: Any) -> int:
        raise ValueError("PubChemLite publishes versioned releases rather than an incremental feed; reinstall to adopt a newer one")

    def accepts(self, value: str) -> bool:
        if value.startswith("pubchem:"):
            return value[len("pubchem:"):].strip().isdigit()
        return "/compound/" in value and "pubchem.ncbi.nlm.nih.gov" in value

    def normalize(self, value: str) -> str:
        if value.startswith("pubchem:"):
            cid = value[len("pubchem:"):]
        else:
            cid = value.split("/compound/", 1)[1].split("#", 1)[0].split("?", 1)[0]
        cid = cid.strip().rstrip("/")
        if not cid.isdigit() or len(cid) > MAX_CID_DIGITS or int(cid) <= 0:
            raise ValueError("id must be a PubChem compound identifier such as pubchem:2244")
        return f"pubchem:{int(cid)}"

    def describe(self, subject: str) -> tuple[str | None, str | None]:
        name = ANNOTATION_NAMES.get(subject)
        return (SUBJECT_GROUP, name) if name else (None, None)
