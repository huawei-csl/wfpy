# wfpy in one page

A workflow file is plain Python: node classes, and one `@workflow` function that wires them.

```python
from wfpy import Port, action, connect, task, workflow


@task
class Start:
    class Ports:
        Out = Port[int](direction="out")

    class Schedule:                # a node with NO input port needs this latch,
        initial = "armed"          # or it fires without end: one transition out
        transitions = [("armed", "emit", "spent")]   # of the first state

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> int:
        return 20


@task
class Add:
    step: int                      # annotated, no default: a parameter, Add(step=2)

    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Sum = Port[int](direction="out")

    @action(consumes={"A": 1, "B": 1}, produces={"Sum": 1})
    def add(self, a: int, b: int) -> int:     # one argument per consumed port, in that order
        return a + b + self.step


@workflow(outputs={"Total": int})
def flow():
    left, right, add = Start(), Start(), Add(step=2)
    connect(left.Out, add.A)
    connect(right.Out, add.B)
    connect(add.Sum, "Total")      # a string names an output of the workflow
```

Rules that are silent failures when broken:

- Always state `direction="in"` or `direction="out"` on a port.
- An input port takes exactly ONE edge. To join N values, give the joining node N input
  ports and consume them all in one action.
- An output port may feed several input ports; each gets its own copy.
- A dict returned from an action is read as `{port name: value}`. To send a dict as the
  token of the port `Out`, return `{"Out": the_dict}`.
- A field with a default, or whose name starts with `_`, is per-node state, not a
  parameter. A parameter has no default.
- The name in a `Schedule` transition is the name of the action's method.
- Create the node instances inside the workflow function, in plain local variables.
- Define exactly one `@workflow` function in the file. No `if __name__ == "__main__":`
  block and no `sys.argv`: the file is imported by `wfpy run`, not run as a script.
- Import only `wfpy` and the Python standard library, unless the task names another module.
