"""The storage gate: an unavailable durable store is stated, never substituted.

``backend/main.py`` wrapped ``manifest.load_project()`` in ``except Exception``
and fell through to the disk manifest on any failure. Two different answers
arrived as one:

* **not found** -- the store answered and holds no document for this project.
  Legitimate, and the disk manifest is the right answer.
* **unavailable** -- the store did not answer at all. Not legitimate, and the
  disk manifest is not evidence about the project.

Both produced a healthy 200 over the disk copy, which on Cloud Run can be the
repo baked into the image (``COPY . .`` under ``/app``) and so is ephemeral. The
symptom was live in production: every ``/api/roughcut/plan`` call logged
``Firestore load_project failed: 404`` and returned 200 anyway.

The reproduction here is not "no database exists" -- that was only how it was
noticed. It is the honest condition underneath: **the store cannot answer, and
we must not serve local state as though it were authoritative.**

Assertion order is deliberate throughout. The assertion that fails under the
original defect runs FIRST in every test, so no cheaper assertion can fail ahead
of it and report a pass-for-the-wrong-reason as coverage.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import config, manifest, projects  # noqa: E402
from backend import main as M  # noqa: E402


# --- doubles --------------------------------------------------------------------
#
# The real client is not installed in this interpreter (conftest documents that
# as deliberate), so the store is stubbed at the seam load_project actually uses.
# Each double fails at a DIFFERENT point, because the two points have different
# consequences: a project read that dies returns nothing, while a beats stream
# that dies mid-iteration returns a storyboard that has lost beats -- which is
# indistinguishable from one that never had them.


class _Unreachable:
    """Every read raises, as an unreachable/absent/denied backend does."""

    def __init__(self, exc=None):
        self._exc = exc or RuntimeError("404 The database (default) does not exist")

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        raise self._exc


class _DeadBeatsStream:
    """The project document reads; its beats subcollection dies mid-stream.

    The partial-read case. `stream()` is lazy, so the failure lands during
    iteration -- after the project document has already come back clean.
    """

    def __init__(self, project_data, beats_before_failure=2):
        self._project = project_data
        self._n = beats_before_failure

    # -- db --
    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        return self

    # -- DocumentSnapshot --
    @property
    def exists(self):
        return True

    def to_dict(self):
        return self._project

    # -- beats subcollection --
    def order_by(self, _field):
        return self

    def stream(self):
        def gen():
            for i in range(self._n):
                yield _Snap({"scene_id": f"s{i + 1:03d}", "narration": "kept"})
            raise RuntimeError("503 Deadline exceeded reading beats")

        return gen()


class _Snap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _Empty:
    """The store answers, and holds no document for this project."""

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        return self

    @property
    def exists(self):
        return False


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose DISK manifest holds recognisable, non-empty state.

    The title is what a substituted answer would leak, so it is the string every
    "did not substitute" assertion looks for.
    """
    root = tmp_path / "bestiary" / "leshy"
    root.mkdir(parents=True)
    mf = root / "storyboard_manifest.json"
    manifest.save(
        manifest.Storyboard(
            title="ON-DISK-ONLY",
            channel="bestiary",
            shots=[manifest.Shot(scene_id="s001", narration="from disk")],
        ),
        mf,
    )
    ctx = projects.ProjectContext.from_manifest(mf)
    monkeypatch.setattr(config, "MANIFEST_PATH", mf)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(mf))
    # The active-project pointer defaults to Path(".active_project") -- the
    # REPOSITORY ROOT when /gcs is absent, which is every workstation. The
    # create-path tests below call set_active_manifest_path for real, so without
    # this they write that file into the checkout and leave it behind: the same
    # test-debris-in-the-workspace class as prompt_ledger.jsonl, reintroduced by
    # the tests that close a storage defect. Same monkeypatch three other test
    # modules already use (test_gates_and_writes.py:76, test_project_isolation.py:331).
    monkeypatch.setattr(M, "ACTIVE_PROJECT_FILE", tmp_path / ".active_project")
    return ctx


@pytest.fixture
def client(project):
    return TestClient(M.app, raise_server_exceptions=False), project


# --- 1. the manifest layer tells the two cases apart -----------------------------

def test_an_unreachable_store_raises_rather_than_reporting_no_record(monkeypatch):
    """The headline. `None` here is what the caller reads as "no document"."""
    monkeypatch.setattr(manifest, "db", _Unreachable())

    with pytest.raises(manifest.StorageUnavailable):
        manifest.load_project("leshy")


def test_a_dead_beats_stream_is_unavailable_not_a_shorter_storyboard(monkeypatch):
    """A partial read must not surface as a storyboard that lost its beats.

    This is the arm that a fix could plausibly miss: the project document reads
    cleanly, so the obvious guard (wrap the `.get()`) leaves this path returning
    a Storyboard with a truncated `shots` list. `save_project` then deletes every
    beat document absent from what it writes, so the truncation becomes durable.
    """
    monkeypatch.setattr(
        manifest, "db", _DeadBeatsStream({"title": "T", "channel": "bestiary"}))

    with pytest.raises(manifest.StorageUnavailable):
        manifest.load_project("leshy")


def test_no_document_is_still_none(monkeypatch):
    """Not-found is a legitimate answer and must NOT have become an error.

    The failure this guards is over-correction: a fix that raises on everything
    breaks the ordinary case where a project simply has no Firestore record yet,
    which is how every project starts.
    """
    monkeypatch.setattr(manifest, "db", _Empty())

    assert manifest.load_project("leshy") is None


def test_an_unconfigured_firestore_is_not_an_outage(monkeypatch):
    """Local development. `db is None` is decided once, at import, by design.

    Raising here would take every workstation checkout offline -- Firestore is
    legitimately absent locally and the JSON manifest is the store of record,
    not a fallback.
    """
    monkeypatch.setattr(manifest, "db", None)

    assert manifest.load_project("leshy") is None


def test_a_parse_defect_is_not_reported_as_a_storage_outage(monkeypatch):
    """Mislabelling sends whoever reads the message to look at infrastructure.

    `from_dict` sits outside the classification guard on purpose: a failure
    there is a defect in manifest.py, not evidence about the store.
    """
    class _Doc:
        def collection(self, _n): return self
        def document(self, _d): return self
        def get(self): return self
        @property
        def exists(self): return True
        def to_dict(self): raise ValueError("not the store's fault")
        def order_by(self, _f): return self
        def stream(self): return iter(())

    monkeypatch.setattr(manifest, "db", _Doc())

    with pytest.raises(ValueError):
        manifest.load_project("leshy")


# --- 2. the caller handles them differently --------------------------------------

def test_get_current_project_refuses_rather_than_serving_the_disk_copy(project):
    """THE DEFECT. An unreachable store used to answer with the disk manifest.

    First assertion is the one the defect fails: with the old
    `except Exception -> fall through`, this call RETURNS a Storyboard instead of
    raising, so `pytest.raises` fails before anything else is checked.
    """
    with projects.use(project):
        original = manifest.db
        manifest.db = _Unreachable()
        try:
            with pytest.raises(HTTPException) as caught:
                M.get_current_project()
        finally:
            manifest.db = original

    exc = caught.value
    assert exc.status_code == 503
    assert exc.detail["storage_gate"] == "unavailable"
    # And the refusal is not quietly carrying the substituted state anyway.
    assert "ON-DISK-ONLY" not in str(exc.detail)


def test_no_record_still_falls_back_to_the_disk_manifest(project):
    """The legitimate case, unchanged. This is also every local run.

    Asserted through the real caller rather than through load_project, because
    the fix lives in the caller and an over-correction there (refusing on `None`
    as well) is the most likely way to break it.
    """
    with projects.use(project):
        original = manifest.db
        manifest.db = _Empty()
        try:
            sb = M.get_current_project()
        finally:
            manifest.db = original

    assert sb.title == "ON-DISK-ONLY"
    assert [s.scene_id for s in sb.shots] == ["s001"]


# --- 3. the write side, which is the same defect ---------------------------------

def test_a_save_that_never_reached_the_store_is_not_reported_as_a_save(project):
    """`save_current_project` printed a warning and carried on to the JSON write.

    So during the outage the studio answered 200 to every edit while nothing was
    persisted anywhere that survives a cold start.
    """
    before = project.manifest_path.read_text(encoding="utf-8")

    with projects.use(project):
        original = manifest.db
        manifest.db = _Unreachable()
        try:
            with pytest.raises(HTTPException) as caught:
                M.save_current_project(manifest.Storyboard(title="EDIT", id=project.project_id))
        finally:
            manifest.db = original

    assert caught.value.status_code == 503
    # The mirror is not half-written: leaving the two stores disagreeing about a
    # project nobody has been told is in trouble is the failure, not the cure.
    assert project.manifest_path.read_text(encoding="utf-8") == before


def test_saving_locally_still_works_with_no_firestore_configured(project):
    """The local-development path must stay exactly as it was."""
    with projects.use(project):
        original = manifest.db
        manifest.db = None
        try:
            M.save_current_project(manifest.Storyboard(title="EDIT", id=project.project_id))
        finally:
            manifest.db = original

    assert manifest.load(project.manifest_path).title == "EDIT"


# --- 4. through the API, where the defect was actually observed ------------------

def test_the_studio_boot_read_refuses_instead_of_answering_200(client):
    """`/api/project/active` is what the studio loads a film with.

    The recorded production symptom was a 200 carrying disk state. The first
    assertion is therefore the status: under the defect this is 200 and fails
    here, before any assertion about the body can pass for the wrong reason.
    """
    c, _ = client
    original = manifest.db
    manifest.db = _Unreachable()
    try:
        r = c.get("/api/project/active")
    finally:
        manifest.db = original

    assert r.status_code == 503, r.text
    # Not flattened into a generic 500 by the handler's `except Exception` arm --
    # the client has to be able to tell "retry, the store is down" from "this
    # server is broken".
    assert r.json()["detail"]["storage_gate"] == "unavailable"
    assert "ON-DISK-ONLY" not in r.text


def test_the_refusal_names_the_project_it_is_about(client):
    c, ctx = client
    original = manifest.db
    manifest.db = _Unreachable()
    try:
        r = c.get("/api/project/active")
    finally:
        manifest.db = original

    assert r.json()["detail"]["project_id"] == ctx.project_id


def test_a_second_endpoint_refuses_the_same_way(client):
    """Not one patched handler: the gate is at the shared read.

    `/api/stages` is the spine the studio draws its navigation from, and it
    reaches the store through the same `get_current_project`.
    """
    c, _ = client
    original = manifest.db
    manifest.db = _Unreachable()
    try:
        r = c.get("/api/stages")
    finally:
        manifest.db = original

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["storage_gate"] == "unavailable"


def test_creating_a_project_is_refused_when_the_store_cannot_record_it(client, tmp_path,
                                                                       monkeypatch):
    """/api/project/new carried the pre-fix write arm verbatim.

    It is the one path that does not read before it writes, so the read gate
    never covered it: during an outage it wrote the JSON manifest, swallowed the
    durable failure, made the new project active and answered 200 {ok: true} --
    then the studio's very next read hit the storage gate.

    Status first: under the defect this is 200, so it fails before any assertion
    about the body or the disk can pass for the wrong reason.
    """
    c, _ = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)
    original = manifest.db
    manifest.db = _Unreachable()
    try:
        r = c.post("/api/project/new", json={"name": "leshy2", "channel": "bestiary"})
    finally:
        manifest.db = original

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["storage_gate"] == "unavailable"


def test_a_refused_create_leaves_no_project_behind(client, tmp_path, monkeypatch):
    """Ordering, not just the arm.

    Writing the JSON first and refusing afterwards leaves a manifest on disk
    with no durable record -- and _scan_projects lists it, so the user is told
    the create failed and then watches the project appear in the sidebar. The
    durable write goes first precisely so a refusal leaves nothing that anything
    lists.
    """
    c, _ = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)
    original = manifest.db
    manifest.db = _Unreachable()
    try:
        c.post("/api/project/new", json={"name": "leshy2", "channel": "bestiary"})
    finally:
        manifest.db = original

    # The manifest is what makes a directory a project -- an empty directory is
    # invisible to _scan_projects and to the bootstrap. Named exactly, because
    # the fixture's own project lives under this tmp_path too and a broad glob
    # would find that one and pass on the wrong evidence.
    assert not (tmp_path / "bestiary" / "leshy2" / "storyboard_manifest.json").exists()


def test_creating_a_project_still_works_with_no_firestore_configured(client, tmp_path,
                                                                     monkeypatch):
    """The local path, which is every workstation and the whole suite."""
    c, _ = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)
    original = manifest.db
    manifest.db = None
    try:
        r = c.post("/api/project/new", json={"name": "leshy2", "channel": "bestiary"})
    finally:
        manifest.db = original

    assert r.status_code == 200, r.text
    assert (tmp_path / "bestiary" / "leshy2" / "storyboard_manifest.json").is_file()


# --- 4b. paid work is recorded even when the save is refused --------------------

def test_a_paid_draft_is_recorded_even_when_the_save_is_refused(client, monkeypatch):
    """Money spent, no record -- §11.4, the write-side form of §11.2.

    ``save_current_project`` deliberately does not write the JSON mirror when
    the durable store is unreachable. The cost lands on the paid callers, which
    spend and THEN save: an outage beginning in that window left images on disk
    with nothing anywhere recording that they were bought, a 503 at the caller,
    and a retry that pays again.

    The store here answers the read (no document -> disk) and fails the write,
    which is the exact window: an outage that begins after the handler started.

    First assertion is the ledger. Under the regression there is no attempt file
    at all, so it fails on the missing record rather than on the status -- which
    is the same either way.
    """
    from backend import generation

    c, _ = client

    def _fake_generate(shot, n=3, backend="", render=None, **kw):
        shot.draft_variations = ["assets/s001/var_a.png", "assets/s001/var_b.png"]
        shot.draft_image = shot.draft_variations[0]
        return shot.draft_variations

    monkeypatch.setattr(M.assets, "generate_for_shot", _fake_generate)

    original = manifest.db
    # Reads answer (no document -> disk); the write raises, because _Empty has
    # no `set`. Exactly "the store went down after this request started".
    manifest.db = _Empty()
    try:
        r = c.post("/api/regenerate/s001", json={"backend": "nano2"})
    finally:
        manifest.db = original

    attempts = generation.load_attempts("s001")
    assert attempts, "a paid draft was generated and nothing recorded it"
    paid = [a for a in attempts if a.paid]
    assert len(paid) == 1
    assert paid[0].kind == "image"
    # Two variations came back, so the estimate opened at one image is revised to
    # two on settle -- our figure, revised, not a bill. fal reports no billed
    # amount on this path, so `cost` stays 0 and `estimated_cost` is what
    # spend() counts. Asserting DRAFT_IMAGE_COST flat would assert that the
    # second image was free.
    assert paid[0].estimated_cost == pytest.approx(
        M.DRAFT_IMAGE_COST * 2, abs=0.001)
    assert paid[0].cost == 0.0, "our own estimate was booked as a provider bill"
    # And the caller was still told the truth about the durable store.
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["storage_gate"] == "unavailable"


def test_recording_a_draft_never_gates_a_deliberate_re_draft(client, monkeypatch):
    """Recording, not gating -- and the distinction is load-bearing.

    begin()'s reuse and in_flight arms are skipped by leaving `signature` and
    `idempotency_key` empty. Enabling them here would refuse the user asking for
    new variations of a beat they already have, which is the legitimate re-buy
    S4-01's remediation was reverted for. Two regenerations in a row must both
    generate, and must produce two attempts rather than one reused.
    """
    from backend import generation

    c, _ = client
    calls = []

    def _fake_generate(shot, n=3, backend="", render=None, **kw):
        calls.append(backend)
        shot.draft_variations = ["assets/s001/var_a.png"]
        return shot.draft_variations

    monkeypatch.setattr(M.assets, "generate_for_shot", _fake_generate)

    original = manifest.db
    manifest.db = None          # local development: saves succeed
    try:
        c.post("/api/regenerate/s001", json={"backend": "nano2"})
        c.post("/api/regenerate/s001", json={"backend": "nano2"})
    finally:
        manifest.db = original

    assert len(calls) == 2, "the second re-draft was refused rather than generated"
    assert len(generation.load_attempts("s001")) == 2


def test_a_draft_that_raises_is_left_in_doubt_not_marked_failed(client, monkeypatch):
    """The provider may already have been called and charged.

    Same rule the paid video path uses: after dispatch, an exception means the
    outcome is unknown, not that nothing was bought.
    """
    from backend import generation

    c, _ = client

    def _boom(shot, n=3, backend="", render=None, **kw):
        raise RuntimeError("fal timed out after dispatch")

    monkeypatch.setattr(M.assets, "generate_for_shot", _boom)

    original = manifest.db
    manifest.db = None
    try:
        c.post("/api/regenerate/s001", json={"backend": "nano2"})
    finally:
        manifest.db = original

    attempts = generation.load_attempts("s001")
    assert attempts, "a dispatched draft left no record at all"
    assert attempts[-1].outcome_unknown is True
    assert attempts[-1].paid is True


# --- 5. the refusal is readable by anyone, so it says nothing about the estate ---

def test_the_refusal_does_not_publish_what_the_store_is(client, capsys):
    """These endpoints are unauthenticated GETs.

    ``require_studio_key`` only enforces X-Studio-Key on non-GET methods, and
    the exception underneath this refusal is a google.cloud one whose text names
    the GCP project and database -- or the service account, on a permission
    failure. page.tsx renders whatever arrives verbatim, so str(exc) here is
    published to anyone with the URL.

    The leak assertion runs first: it is the one that fails if the body goes
    back to carrying the exception.
    """
    c, _ = client
    original = manifest.db
    manifest.db = _Unreachable(RuntimeError(
        "403 Permission denied on project my-gcp-project-42 "
        "for serviceAccount:studio@my-gcp-project-42.iam.gserviceaccount.com"))
    try:
        r = c.get("/api/project/active")
    finally:
        manifest.db = original

    body = r.text
    for secret in ("my-gcp-project-42", "serviceAccount", "iam.gserviceaccount.com",
                   "Permission denied"):
        assert secret not in body, f"{secret!r} was published in the refusal body"
    # Still states the block rather than saying nothing.
    assert r.json()["detail"]["error"] == M.STORAGE_GATE_MESSAGE
    assert r.json()["detail"]["storage_gate"] == "unavailable"


def test_the_diagnosis_is_relocated_to_the_log_not_lost(client, capsys):
    """Withholding it from the body only works if the operator can still read it.

    Same line pipeline_worker draws for the other public GET: the detail goes to
    stdout, which is Cloud Logging, and never into a public response.
    """
    c, _ = client
    original = manifest.db
    manifest.db = _Unreachable(RuntimeError("403 Permission denied on my-gcp-project-42"))
    try:
        c.get("/api/project/active")
    finally:
        manifest.db = original

    logged = capsys.readouterr().out
    assert "my-gcp-project-42" in logged
    assert "Storage gate:" in logged


def test_an_ordinary_request_is_unaffected_when_the_store_has_no_record(client):
    """The whole suite runs in this mode, and so does every local checkout."""
    c, _ = client
    original = manifest.db
    manifest.db = _Empty()
    try:
        r = c.get("/api/project/active")
    finally:
        manifest.db = original

    assert r.status_code == 200, r.text
    assert r.json()["project"]["title"] == "ON-DISK-ONLY"
