import { useState } from "react";
import type { FaceIdentity, FaceListType } from "../api/faces";
import {
  addFacePhotos,
  createFaceIdentity,
  deleteFaceIdentity,
  listFaceIdentities,
  updateFaceIdentity,
} from "../api/faces";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Layout, relativeTime } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import { EmptyPanel, FailureState, InlineSpinner, LoadingRows } from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Face recognition: the people this organisation has registered.
 *
 *  A face is biometric data, so registering someone is deliberately deliberate. The person
 *  must have agreed (recorded with a note and a timestamp), photos are turned into an
 *  encrypted template and then thrown away (nothing image-shaped is kept), and removing a
 *  person destroys their templates immediately. The page says all of that out loud rather
 *  than leaving it to a privacy policy nobody opens.
 *
 *  Two lists: **known** people are recognised by name on camera and raise no alert;
 *  **watchlist** people raise an alert whenever they are seen. Anyone not registered is
 *  "unknown", which a rule can alert on. */

const LIST_LABEL: Record<FaceListType, string> = { known: "Known", watchlist: "Watchlist" };
const LIST_PILL: Record<FaceListType, string> = { known: "pill-ready", watchlist: "pill-provisioning" };

export function FacesPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();
  const people = useResource(listFaceIdentities, []);

  const [registering, setRegistering] = useState(false);
  const [addingPhotosFor, setAddingPhotosFor] = useState<FaceIdentity | null>(null);
  const [deleting, setDeleting] = useState<FaceIdentity | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteFaceIdentity(removed.id);
      people.mutate((current) => (current ?? []).filter((p) => p.id !== removed.id));
      notify.success(`${removed.display_name} was removed`, "Their stored face templates were destroyed.");
      setDeleting(null);
    } catch (err) {
      notify.error(`Could not remove ${removed.display_name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setDeleteBusy(false);
    }
  }

  async function toggleList(person: FaceIdentity) {
    const next: FaceListType = person.list_type === "known" ? "watchlist" : "known";
    try {
      const updated = await updateFaceIdentity(person.id, { list_type: next });
      people.mutate((current) => (current ?? []).map((p) => (p.id === updated.id ? updated : p)));
    } catch (err) {
      notify.error(`Could not update ${person.display_name}`, err instanceof Error ? err.message : undefined);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Faces</h1>
          <p className="muted">
            {people.data ? `${people.data.length} registered ${people.data.length === 1 ? "person" : "people"}` : " "}
            {people.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setRegistering(true)} disabled={!online}>
          Register a person
        </button>
      </div>

      <div className="notice notice-info" style={{ marginBottom: 16 }}>
        <span aria-hidden="true">ⓘ</span>
        <div>
          <strong>How faces are handled.</strong>
          <p>
            Only register people who have agreed. Photos are converted to an encrypted template
            and discarded; no photo is stored. Removing a person destroys their template. New
            registrations take effect on cameras within about a minute.
          </p>
        </div>
      </div>

      {people.loading ? (
        <LoadingRows rows={4} columns={5} />
      ) : Boolean(people.error) && !people.data ? (
        <FailureState error={people.error} online={online} onRetry={people.reload} entity="registered people" />
      ) : (people.data ?? []).length === 0 ? (
        <EmptyPanel
          title="No one is registered yet"
          icon="☺"
          action={
            <button type="button" onClick={() => setRegistering(true)}>
              Register the first person
            </button>
          }
        >
          <p>
            Register a person with one or more clear photos of their face, and cameras running
            face recognition will recognise them by name.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Registered people</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">List</th>
                <th scope="col">Photos</th>
                <th scope="col">Consent recorded</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(people.data ?? []).map((person) => (
                <tr key={person.id}>
                  <td>
                    <strong>{person.display_name}</strong>
                    {person.external_ref && <div className="muted mono">{person.external_ref}</div>}
                  </td>
                  <td>
                    <span className={`pill ${LIST_PILL[person.list_type]}`}>{LIST_LABEL[person.list_type]}</span>
                  </td>
                  <td>
                    {person.template_count === 0 ? (
                      <span className="muted">None - cannot be recognised yet</span>
                    ) : (
                      person.template_count
                    )}
                  </td>
                  <td title={person.consent_note}>{relativeTime(person.consent_recorded_at)}</td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => setAddingPhotosFor(person)} disabled={!online}>
                      Add photos
                    </button>
                    <button type="button" className="btn-quiet" onClick={() => void toggleList(person)} disabled={!online}>
                      {person.list_type === "known" ? "Move to watchlist" : "Move to known"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(person)}
                      disabled={!online}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {registering && (
        <RegisterDialog
          onClose={() => setRegistering(false)}
          onRegistered={() => {
            setRegistering(false);
            people.reload();
            notify.success("Person registered", "Cameras will recognise them within about a minute.");
          }}
          onPartial={() => people.reload()}
        />
      )}

      {addingPhotosFor && (
        <PhotosDialog
          person={addingPhotosFor}
          onClose={() => setAddingPhotosFor(null)}
          onAdded={() => {
            setAddingPhotosFor(null);
            people.reload();
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting?.display_name ?? "this person"}?`}
        busy={deleteBusy}
        confirmLabel="Remove and destroy templates"
        body={
          <p>
            <strong>{deleting?.display_name}</strong> will no longer be recognised, and their stored
            face templates are destroyed immediately. This cannot be undone.
          </p>
        }
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </Layout>
  );
}

function PhotoPicker({ files, onChange }: { files: File[]; onChange: (files: File[]) => void }) {
  return (
    <div className="field">
      <label htmlFor="face-photos">Photos (1 to 5)</label>
      <p className="field-hint" id="face-photos-hint">
        One clear, well-lit, front-facing photo of just this person per file. More than one
        photo from slightly different angles improves recognition.
      </p>
      <input
        id="face-photos"
        aria-describedby="face-photos-hint"
        type="file"
        accept="image/jpeg,image/png"
        multiple
        capture="user"
        onChange={(e) => onChange(Array.from(e.target.files ?? []).slice(0, 5))}
      />
      {files.length > 0 && <p className="muted">{files.length} selected</p>}
    </div>
  );
}

function RegisterDialog({
  onClose,
  onRegistered,
  onPartial,
}: {
  onClose: () => void;
  onRegistered: () => void;
  onPartial: () => void;
}) {
  const [name, setName] = useState("");
  const [reference, setReference] = useState("");
  const [listType, setListType] = useState<FaceListType>("known");
  const [consent, setConsent] = useState(false);
  const [consentNote, setConsentNote] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // If the person is created but a photo is rejected, keep the id so a retry adds photos
  // to the same person instead of creating a duplicate.
  const [createdId, setCreatedId] = useState<string | null>(null);

  const ready = name.trim() !== "" && consent && consentNote.trim().length >= 3 && files.length > 0;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      let id = createdId;
      if (id === null) {
        const created = await createFaceIdentity({
          display_name: name.trim(),
          external_ref: reference.trim() || null,
          list_type: listType,
          consent,
          consent_note: consentNote.trim(),
        });
        id = created.id;
        setCreatedId(id);
        onPartial();
      }
      await addFacePhotos(id, files);
      onRegistered();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open title="Register a person" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        {error && (
          <div className="notice notice-error" role="alert" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>{error}</strong>
              {createdId && <p>The person is saved. Choose a different photo and try again.</p>}
            </div>
          </div>
        )}

        <div className="field">
          <label htmlFor="face-name">Name</label>
          <input id="face-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} disabled={createdId !== null} />
        </div>
        <div className="field">
          <label htmlFor="face-ref">Reference (optional)</label>
          <input id="face-ref" value={reference} onChange={(e) => setReference(e.target.value)} placeholder="Staff ID" maxLength={120} disabled={createdId !== null} />
        </div>
        <div className="field">
          <label htmlFor="face-list">List</label>
          <select id="face-list" value={listType} onChange={(e) => setListType(e.target.value as FaceListType)} disabled={createdId !== null}>
            <option value="known">Known - recognised by name, no alert</option>
            <option value="watchlist">Watchlist - alert whenever seen</option>
          </select>
        </div>

        <PhotoPicker files={files} onChange={setFiles} />

        <div className="field">
          <label>
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} disabled={createdId !== null} />{" "}
            This person has agreed to be registered for face recognition.
          </label>
        </div>
        <div className="field">
          <label htmlFor="face-consent-note">How was consent given?</label>
          <input
            id="face-consent-note"
            value={consentNote}
            onChange={(e) => setConsentNote(e.target.value)}
            placeholder="Signed staff form, 24 Sep 2026"
            maxLength={500}
            disabled={createdId !== null}
          />
        </div>

        <div className="form-actions">
          <button type="button" className="btn-quiet" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" disabled={!ready || busy}>
            {busy ? "Registering…" : "Register"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function PhotosDialog({
  person,
  onClose,
  onAdded,
}: {
  person: FaceIdentity;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await addFacePhotos(person.id, files);
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open title={`Add photos for ${person.display_name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        {error && (
          <div className="notice notice-error" role="alert" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>{error}</strong>
            </div>
          </div>
        )}
        <PhotoPicker files={files} onChange={setFiles} />
        <div className="form-actions">
          <button type="button" className="btn-quiet" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" disabled={files.length === 0 || busy}>
            {busy ? "Adding…" : "Add photos"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
