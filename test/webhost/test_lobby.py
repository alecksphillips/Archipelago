from datetime import timedelta
from io import BytesIO

from flask import url_for
from pony.orm import db_session

from . import TestBase


def make_yaml(name: str) -> bytes:
    return f"name: {name}\ngame: Archipelago\nArchipelago: {{}}\n".encode("utf-8")


class TestLobby(TestBase):
    def create_lobby(self, client, name: str = "test lobby") -> str:
        """Creates a lobby and returns the path to it."""
        response = client.post(url_for("lobby_index"), data={"name": name})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/lobby/", response.location)
        return response.location

    def upload(self, client, lobby_path: str, *files: tuple[str, bytes], follow: bool = True):
        data = {"file": [(BytesIO(content), filename) for filename, content in files]}
        return client.post(f"{lobby_path}/upload", data=data, follow_redirects=follow)

    def test_upload_and_persistence(self) -> None:
        """YAMLs from several users end up in one lobby and are still there for people who come back later."""
        with self.app.test_request_context():
            alice, bob = self.client, self.app.test_client()
            lobby = self.create_lobby(alice)

            self.assertIn(b"test lobby", alice.get(lobby).data)
            self.assertIn(b"alice.yaml", self.upload(alice, lobby, ("alice.yaml", make_yaml("Alice"))).data)
            # a different session can see and add to the lobby
            response = self.upload(bob, lobby, ("bob.yaml", make_yaml("Bob")))
            self.assertIn(b"alice.yaml", response.data)
            self.assertIn(b"bob.yaml", response.data)

            # same filename gets renamed instead of overwriting or failing
            response = self.upload(bob, lobby, ("bob.yaml", make_yaml("Bob2")))
            self.assertIn(b"bob (2).yaml", response.data)

            # a brand new session (e.g. next day, other device) still sees everything: data lives in the db
            response = self.app.test_client().get(lobby)
            self.assertEqual(response.status_code, 200)
            for name in (b"alice.yaml", b"bob.yaml", b"bob (2).yaml"):
                self.assertIn(name, response.data)

    def test_invalid_yaml_is_rejected(self) -> None:
        with self.app.test_request_context():
            lobby = self.create_lobby(self.client)
            response = self.upload(self.client, lobby, ("broken.yaml", b"name: Broken\ngame: Not A Real Game\n"))
            self.assertIn(b"Failed to generate options in broken.yaml", response.data)
            self.assertNotIn(b"lobby-yamls", response.data)
            # wrong extension / banned file
            response = self.upload(self.client, lobby, ("notes.exe", b"hi"))
            self.assertIn(b"Did not find any valid files", response.data)

    def test_zip_upload(self) -> None:
        import zipfile
        with self.app.test_request_context():
            lobby = self.create_lobby(self.client)
            buffer = BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                zf.writestr("folder/one.yaml", make_yaml("One"))
                zf.writestr("two.yaml", make_yaml("Two"))
            response = self.upload(self.client, lobby, ("pack.zip", buffer.getvalue()))
            self.assertIn(b"one.yaml", response.data)
            self.assertIn(b"two.yaml", response.data)

    def test_lobby_full(self) -> None:
        with self.app.test_request_context():
            self.app.config["LOBBY_MAX_YAMLS"] = 2
            try:
                lobby = self.create_lobby(self.client)
                response = self.upload(self.client, lobby, *((f"p{i}.yaml", make_yaml(f"P{i}")) for i in range(3)))
                self.assertIn(b"This lobby is full", response.data)
                self.assertIn(b"<td>p0.yaml</td>", response.data)
                self.assertIn(b"<td>p1.yaml</td>", response.data)
                self.assertNotIn(b"<td>p2.yaml</td>", response.data)
            finally:
                self.app.config["LOBBY_MAX_YAMLS"] = 50

    def test_remove_permissions(self) -> None:
        from WebHostLib import to_url
        from WebHostLib.models import LobbyYaml
        with self.app.test_request_context():
            alice, bob, carol = self.client, self.app.test_client(), self.app.test_client()
            lobby = self.create_lobby(alice)  # alice owns the lobby
            self.upload(bob, lobby, ("bob.yaml", make_yaml("Bob")))
            self.upload(carol, lobby, ("carol.yaml", make_yaml("Carol")))
            with db_session:
                ids = {y.filename: y.id for y in LobbyYaml.select()}

            def delete_path(filename: str) -> str:
                return f"{lobby}/delete/{to_url(ids[filename])}"

            # carol can't remove bob's file, bob can remove his own, the lobby owner can remove anything
            self.assertEqual(carol.post(delete_path("bob.yaml")).status_code, 403)
            self.assertEqual(bob.post(delete_path("bob.yaml")).status_code, 302)
            self.assertEqual(alice.post(delete_path("carol.yaml")).status_code, 302)
            with db_session:
                self.assertEqual(LobbyYaml.select().count(), 0)

    def test_generate(self) -> None:
        with self.app.test_request_context():
            alice, bob = self.client, self.app.test_client()
            lobby = self.create_lobby(alice)
            # nothing to generate yet
            response = alice.post(f"{lobby}/generate", follow_redirects=True)
            self.assertIn(b"This lobby has no YAMLs yet", response.data)

            self.upload(alice, lobby, ("alice.yaml", make_yaml("Alice")))
            self.upload(bob, lobby, ("bob.yaml", make_yaml("Bob")))
            # any participant can start the generation
            response = bob.post(f"{lobby}/generate", data={"spoiler": "1", "hint_cost": "10"}, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertTrue("/seed/" in response.request.path or "/wait/" in response.request.path,
                            f"Response did not properly redirect ({response.request.path})")

            from WebHostLib.models import Generation, LobbyYaml
            with db_session:
                # queued generation contains both players; the lobby keeps its files for another round
                self.assertEqual(Generation.select().count(), 1)
                self.assertEqual(LobbyYaml.select().count(), 2)

    def test_too_many_players_returns_to_lobby(self) -> None:
        """start_generation redirects to the requesting endpoint with GET on error, that must not 405."""
        with self.app.test_request_context():
            self.app.config["MAX_ROLL"] = 1
            try:
                lobby = self.create_lobby(self.client)
                self.upload(self.client, lobby, ("a.yaml", make_yaml("A")), ("b.yaml", make_yaml("B")))
                response = self.client.post(f"{lobby}/generate", follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"limited to 1 players", response.data)
                self.assertEqual(response.request.path, lobby)
            finally:
                self.app.config["MAX_ROLL"] = 20

    def test_delete_lobby_and_unknown_lobby(self) -> None:
        from WebHostLib.models import Lobby, LobbyYaml
        with self.app.test_request_context():
            alice, bob = self.client, self.app.test_client()
            lobby = self.create_lobby(alice)
            self.upload(bob, lobby, ("bob.yaml", make_yaml("Bob")))
            self.assertEqual(bob.post(f"{lobby}/delete").status_code, 403)
            self.assertEqual(alice.post(f"{lobby}/delete").status_code, 302)
            with db_session:
                self.assertEqual(Lobby.select().count(), 0)
                self.assertEqual(LobbyYaml.select().count(), 0)
            self.assertEqual(alice.get(lobby).status_code, 404)

    def test_cleanup(self) -> None:
        from Utils import utcnow
        from WebHostLib.autolauncher import cleanup
        from WebHostLib.models import Lobby, LobbyYaml
        with self.app.test_request_context():
            old, new = self.create_lobby(self.client, "old"), self.create_lobby(self.client, "new")
            self.upload(self.client, old, ("old.yaml", make_yaml("Old")))
            self.upload(self.client, new, ("new.yaml", make_yaml("New")))
            with db_session:
                Lobby.get(name="old").last_activity = utcnow() - timedelta(days=10)

            cleanup({"LOBBY_AUTO_DELETE": 0})  # disabled: nothing happens
            with db_session:
                self.assertEqual(Lobby.select().count(), 2)

            cleanup({"LOBBY_AUTO_DELETE": 5})
            with db_session:
                self.assertEqual([l.name for l in Lobby.select()], ["new"])
                self.assertEqual([y.filename for y in LobbyYaml.select()], ["new.yaml"])
