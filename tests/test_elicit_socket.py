"""The socket elicitation channel: an IDE answers the agents' questions on a
Unix socket, one JSON object a line."""

from __future__ import annotations

import json
import socket
import threading

from wfpy._elicitation_runtime import (ElicitationRequest, SocketElicitationHandler, build_elicit_closure,
                                       resolve_base_handler)


class Listener(threading.Thread):
    """A client of the channel: answers every question with `reply(question)`."""

    def __init__(self, path, reply):
        super().__init__(daemon=True)
        self.path, self.reply, self.seen = path, reply, []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)

    def run(self):
        conn, _ = self.server.accept()
        with conn, conn.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                q = json.loads(line)
                self.seen.append(q)
                answer = self.reply(q)
                if answer is None:
                    return
                conn.sendall((json.dumps(answer) + "\n").encode())


def req(question="Write hello.txt", choices=("Allow", "Reject")):
    return ElicitationRequest(question=question, choices=list(choices), agent_name="planner", model="opus",
                              context="The agent asks permission for this tool call.", timeout_ms=5000)


class TestTheChannel:
    def test_a_question_goes_out_and_the_answer_comes_back(self, tmp_path):
        path = str(tmp_path / "elicit.sock")
        listener = Listener(path, lambda q: {"id": q["id"], "answer": "Reject"})
        listener.start()
        handler = SocketElicitationHandler(path)
        resp = handler(req())
        assert resp.answer == "Reject" and not resp.declined
        sent = listener.seen[0]
        assert sent["type"] == "question" and sent["id"] == 1 and sent["agent"] == "planner"
        assert sent["question"] == "Write hello.txt" and sent["choices"] == ["Allow", "Reject"]
        assert handler(req("second")).answer == "Reject" and listener.seen[1]["id"] == 2   # one connection, serialized

    def test_no_listener_is_a_declined_question_not_an_error(self, tmp_path):
        resp = SocketElicitationHandler(str(tmp_path / "nobody.sock"))(req())
        assert resp.declined and "no listener" in resp.reason

    def test_a_declined_reply_and_a_closed_listener(self, tmp_path):
        path = str(tmp_path / "e.sock")
        answers = iter([{"id": 1, "declined": True, "reason": "user said no"}, None])
        listener = Listener(path, lambda q: next(answers))
        listener.start()
        handler = SocketElicitationHandler(path)
        first = handler(req())
        assert first.declined and first.reason == "user said no"
        second = handler(req())
        assert second.declined and "closed" in second.reason

    def test_a_reply_to_another_question_is_declined(self, tmp_path):
        path = str(tmp_path / "e.sock")
        Listener(path, lambda q: {"id": 99, "answer": "Allow"}).start()
        resp = SocketElicitationHandler(path)(req())
        assert resp.declined and "another question" in resp.reason

    def test_the_run_option_selects_it_and_the_closure_uses_it(self, tmp_path):
        path = str(tmp_path / "e.sock")
        listener = Listener(path, lambda q: {"id": q["id"], "answer": "Allow"})
        listener.start()
        options = {"elicit_socket": path}
        assert isinstance(resolve_base_handler(options), SocketElicitationHandler)
        assert resolve_base_handler(options) is resolve_base_handler(options)      # one handler per run
        ask = build_elicit_closure(options, agent_name="planner", model="opus")
        assert ask("Write hello.txt", choices=["Allow", "Reject"]).answer == "Allow"
        assert listener.seen[0]["model"] == "opus"
