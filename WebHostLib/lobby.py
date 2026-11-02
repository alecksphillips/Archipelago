"""
Persistent YAML lobbies.

A lobby is a server-side, link-shared collection of player YAMLs. Anyone who has the lobby's link can add YAMLs and can
remove the ones they added; the lobby's creator can remove any. Once everybody has uploaded, anyone with the link can
press "Generate", which feeds the stored YAMLs into the same pipeline as the /generate page.

Lobbies are identified by an unguessable UUID, the link is the access control. Identity is the anonymous
`session["_id"]` cookie, like everywhere else in WebHostLib.
"""
import os
from uuid import UUID

from flask import abort, flash, redirect, render_template, request, session, url_for
from pony.orm import desc

from Utils import __version__, utcnow
from WebHostLib import app
from .check import get_yaml_data, roll_options
from .generate import get_meta, start_generation
from .models import Lobby, LobbyYaml

MAX_LOBBIES_PER_OWNER = 25
MAX_YAML_BYTES = 1024 * 1024
MAX_FILENAME_LENGTH = 200
MAX_LOBBY_NAME_LENGTH = 64


def _get_lobby(lobby_id: UUID) -> Lobby:
    lobby = Lobby.get(id=lobby_id)
    if not lobby:
        abort(404)
    return lobby


def _unique_filename(filename: str, taken: set[str]) -> str:
    """Return a variant of filename that is not in taken. Keeps the extension, as the name is used as a fallback
    player name."""
    stem, ext = os.path.splitext(filename)
    stem = stem[:MAX_FILENAME_LENGTH - len(ext) - 6]
    candidate = stem + ext
    counter = 1
    while candidate in taken:
        counter += 1
        candidate = f"{stem} ({counter}){ext}"
    return candidate


@app.route("/lobby", methods=["GET", "POST"])
def lobby_index():
    owner = session["_id"]
    if request.method == "POST":
        if Lobby.select(lambda l: l.owner == owner).count() >= MAX_LOBBIES_PER_OWNER:
            flash(f"You can only have {MAX_LOBBIES_PER_OWNER} lobbies. Please delete one first.")
        else:
            lobby = Lobby(owner=owner, name=request.form.get("name", "").strip()[:MAX_LOBBY_NAME_LENGTH])
            return redirect(url_for("lobby_view", lobby=lobby.id))
    own_lobbies = Lobby.select(lambda l: l.owner == owner).order_by(lambda l: desc(l.last_activity))[:]
    return render_template("lobbyIndex.html", lobbies=own_lobbies)


@app.route("/lobby/<suuid:lobby>")
def lobby_view(lobby: UUID):
    lobby = _get_lobby(lobby)
    me = session["_id"]
    # content is lazy, so this does not pull in the yamls themselves
    yamls = LobbyYaml.select(lambda y: y.lobby == lobby).order_by(lambda y: y.uploaded)[:]
    return render_template("lobby.html", lobby=lobby, yamls=yamls, me=me, is_owner=lobby.owner == me,
                           max_yamls=app.config["LOBBY_MAX_YAMLS"], version=__version__)


@app.route("/lobby/<suuid:lobby>/upload", methods=["POST"])
def lobby_upload(lobby: UUID):
    lobby = _get_lobby(lobby)
    back = redirect(url_for("lobby_view", lobby=lobby.id))
    files = [f for f in request.files.getlist("file") if f.filename]
    if not files:
        flash("No file selected.")
        return back

    options = get_yaml_data(files)  # shared with /generate and /check: handles .zip, banned files, extensions
    if isinstance(options, str):
        flash(options)
        return back

    # reject broken yamls right away, so problems show up while their author is still around
    results, _ = roll_options(options)

    taken = {y.filename for y in LobbyYaml.select(lambda y: y.lobby == lobby)}
    max_yamls: int = app.config["LOBBY_MAX_YAMLS"]
    added = 0
    for name, content in options.items():
        filename = os.path.basename(name)
        if results[name] is not True:
            flash(results[name])
        elif len(content) > MAX_YAML_BYTES:
            flash(f"{filename} is too large (limit is {MAX_YAML_BYTES // 1024} KiB).")
        elif len(taken) >= max_yamls:
            flash(f"This lobby is full ({max_yamls} files). {filename} and any further files were not added.")
            break
        else:
            filename = _unique_filename(filename, taken)
            taken.add(filename)
            LobbyYaml(lobby=lobby, uploader=session["_id"], filename=filename,
                      content=content if isinstance(content, bytes) else content.encode("utf-8"))
            added += 1
    if added:
        lobby.last_activity = utcnow()
    return back


@app.route("/lobby/<suuid:lobby>/delete/<suuid:yaml_id>", methods=["POST"])
def lobby_delete_yaml(lobby: UUID, yaml_id: UUID):
    lobby = _get_lobby(lobby)
    lobby_yaml = LobbyYaml.get(id=yaml_id)
    if not lobby_yaml or lobby_yaml.lobby != lobby:
        abort(404)
    if session["_id"] not in (lobby_yaml.uploader, lobby.owner):
        abort(403)
    lobby_yaml.delete()
    lobby.last_activity = utcnow()
    return redirect(url_for("lobby_view", lobby=lobby.id))


@app.route("/lobby/<suuid:lobby>/delete", methods=["POST"])
def lobby_delete(lobby: UUID):
    lobby = _get_lobby(lobby)
    if session["_id"] != lobby.owner:
        abort(403)
    lobby.delete()  # cascades to its yamls
    return redirect(url_for("lobby_index"))


@app.route("/lobby/<suuid:lobby>/generate", methods=["GET", "POST"])
def lobby_generate(lobby: UUID):
    # GET exists because start_generation redirects back to the requesting endpoint with GET when it has an error
    # to flash, e.g. too many players.
    if request.method == "GET":
        return redirect(url_for("lobby_view", lobby=lobby))
    lobby = _get_lobby(lobby)
    stored = LobbyYaml.select(lambda y: y.lobby == lobby).order_by(lambda y: y.uploaded)[:]
    if not stored:
        flash("This lobby has no YAMLs yet.")
        return redirect(url_for("lobby_view", lobby=lobby.id))
    # filenames are unique within a lobby, see lobby_upload
    options = {y.filename: y.content for y in stored}
    lobby.last_activity = utcnow()
    meta = get_meta(request.form, race=bool(request.form.get("race")))
    return start_generation(options, meta)
