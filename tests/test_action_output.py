"""Output routing for internal task actions.

Regression tests for two silent-no-output bugs:

* A default ``action`` used to emit nothing unless it had a return type
  annotation (#7).
* A multi-output action returning a dict used to lose its values unless
  ``produces=`` was declared (#6).

Both failed silently — the run "succeeded" and downstream ports saw ``None``.
"""

from __future__ import annotations

from wfpy import Port, action, connect, run, task, workflow


def _run(wf, **inputs):
    return run(wf, inputs=inputs or None, verbose=False)


class TestDefaultActionNeedsNoReturnAnnotation:
    """#7 — a plain ``action`` emits on its ports regardless of annotation."""

    def test_unannotated_action_emits_output(self, tmp_path):
        @task
        class Doubler:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, x):  # no return annotation
                return x * 2

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def wf() -> None:
            d = Doubler()
            connect("In", d.In)
            connect(d.Out, "Out")

        assert run(wf, inputs={"In": 21}, out_dir=str(tmp_path), verbose=False) == {
            "Out": [42]
        }

    def test_produces_inferred_from_ports_not_annotation(self):
        @task
        class Doubler:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, x):
                return x * 2

        # The inferred ActionDef produces on the output port even with no hint.
        assert Doubler._wfpy_meta.actions[0].produces == {"Out": 1}

    def test_none_return_still_emits_nothing(self, tmp_path):
        @task
        class Sink:
            _seen: int = 0

            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, x):
                self._seen += x
                return None

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def wf() -> None:
            s = Sink()
            connect("In", s.In)
            connect(s.Out, "Out")

        # Fires (consumes the token) but emits nothing on Out.
        assert run(wf, inputs={"In": 5}, out_dir=str(tmp_path), verbose=False) == {
            "Out": None
        }


class TestMultiOutputDistribution:
    """#6 — a dict/list result is distributed across ports without produces=."""

    def test_default_action_dict_distributes_by_port(self, tmp_path):
        @task
        class Split:
            class Ports:
                In = Port[int](direction="in")
                A = Port[int](direction="out")
                B = Port[int](direction="out")

            def action(self, x):  # default action, no produces=
                return {"A": x * 2, "B": x * 3}

        @workflow(inputs={"In": int}, outputs={"A": int, "B": int})
        def wf() -> None:
            s = Split()
            connect("In", s.In)
            connect(s.A, "A")
            connect(s.B, "B")

        assert run(wf, inputs={"In": 5}, out_dir=str(tmp_path), verbose=False) == {
            "A": [10],
            "B": [15],
        }

    def test_decorated_action_without_produces_dict_distributes(self, tmp_path):
        @task
        class Split:
            class Ports:
                In = Port[int](direction="in")
                A = Port[int](direction="out")
                B = Port[int](direction="out")

            @action  # decorated but no produces=
            def act(self, x: int):
                return {"A": x * 2, "B": x * 3}

        @workflow(inputs={"In": int}, outputs={"A": int, "B": int})
        def wf() -> None:
            s = Split()
            connect("In", s.In)
            connect(s.A, "A")
            connect(s.B, "B")

        assert run(wf, inputs={"In": 5}, out_dir=str(tmp_path), verbose=False) == {
            "A": [10],
            "B": [15],
        }

    def test_decorated_action_without_produces_list_distributes_by_index(self, tmp_path):
        @task
        class Split:
            class Ports:
                In = Port[int](direction="in")
                A = Port[int](direction="out")
                B = Port[int](direction="out")

            @action
            def act(self, x: int):
                return [x * 2, x * 3]

        @workflow(inputs={"In": int}, outputs={"A": int, "B": int})
        def wf() -> None:
            s = Split()
            connect("In", s.In)
            connect(s.A, "A")
            connect(s.B, "B")

        assert run(wf, inputs={"In": 5}, out_dir=str(tmp_path), verbose=False) == {
            "A": [10],
            "B": [15],
        }

    def test_single_output_returning_dict_value_is_preserved(self, tmp_path):
        """A single-output action's dict return is a value, not a port map."""

        @task
        class Wrap:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[dict](direction="out")

            @action  # no produces=, single output
            def act(self, x: int):
                return {"n": x, "doubled": x * 2}

        @workflow(inputs={"In": int}, outputs={"Out": dict})
        def wf() -> None:
            w = Wrap()
            connect("In", w.In)
            connect(w.Out, "Out")

        assert run(wf, inputs={"In": 5}, out_dir=str(tmp_path), verbose=False) == {
            "Out": [{"n": 5, "doubled": 10}]
        }
