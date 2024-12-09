from typing import Callable
from abc import ABC, abstractmethod
import os
from dataclasses import dataclass

class Node(ABC):
    @abstractmethod
    def get_id(self) -> str:
        pass
    
    @abstractmethod
    def get_time(self) -> float | None:
        """Returns the creation time or None if this Node was not created yet"""
        pass

class StaticNode(Node):
    """
    Static nodes exist regardless of this build system.
    They may be used by it, but are not generated or modified by it.
    """
    def _raise_not_exist(self, cause) -> None:
        if cause != "":
            cause = f" : {cause}"
        raise RuntimeError(f"Expected static node '{self.get_id()}' to exist{cause}")

    @abstractmethod
    def _check_exists(self) -> str | None:
        """Return None if exists or str explaining why it does not exist"""
        pass

    def verify_exists(self) -> None:
        """Check if file exists and raise an error if not"""
        res: str | None = self._check_exists()
        if res is not None:
            self._raise_not_exist(res)

class DynamicNode(Node):
    """
    Dynamic nodes are managed by the build system.
    They do not exist when the build system is in it's clean state.
    Each dynamic node in the build system should have a rule to generate it.
    """
    pass

@dataclass
class FileNode(Node):
    path: str

    def get_id(self) -> str:
        return self.path

    def get_time(self) -> float | None:
        try:
            return os.path.getmtime(self.path)
        except FileNotFoundError:
            return None


class DynamicFileNode(DynamicNode, FileNode):
    pass

class StaticFileNode(StaticNode, FileNode):
    def _check_exists(self) -> str | None:
        if not os.path.exists(self.path):
            return "File not found"
        return None

# @dataclass
# class PythonNode(Node):
#     pass

@dataclass
class Rule(ABC):
    """
    'Rule' might be a good place to add resource locks when later adding multiprocessing.
    """
    target: DynamicNode
    depends_on: list[Node]

    def is_up_to_date(self) -> bool:
        res: float | None = self.target.get_time()
        if res is None:
            return False
        dep_times: list[float | None] = [dep.get_time() for dep in self.depends_on]
        return all([isinstance(t, float) and t < res for t in dep_times])

    @abstractmethod
    def execute(self) -> None:
        pass

class ShellRule(Rule):
    gen_cmd: Callable[[Rule], str]

    def __init__(self, target: DynamicNode, deps: list[Node], gen_cmd: Callable[[Rule], str]):
        super().__init__(target, deps)
        self.gen_cmd = gen_cmd

    def execute(self) -> None:
        cmd: str = self.gen_cmd(self)
        print(cmd)
        if os.system(cmd) != 0:
            raise RuntimeError(f"Command '{cmd}' failed, while building target '{self.target.get_id()}'")

@dataclass
class BuildSystem:
    def __init__(self, rules: list[Rule]):
        
        self.rules: dict[str, Rule] = dict()
        self.nodes: dict[str, Node] = dict()

        """
        node_requesters is currently dead code, but when adding multiprocessing in the future,
            node_requesters will be needed to notify that it's requester nodes might be ready to be created.
        """
        self.node_requesters: dict[str, list[str]] = dict() # The key is the node id, and the list is nodes (ids) which depend on it.

        for r in rules:
            # Add to rules:
            tgt_id = r.target.get_id()
            if tgt_id in self.rules.keys():
                raise ValueError(f"Got multiple rules with target: '{tgt_id}'")
            self.rules[tgt_id] = r
            
            # Add to nodes:
            for node in [r.target] + r.depends_on:
                ident: str = node.get_id()
                if ident not in self.nodes.keys():
                    self.nodes[ident] = node
                    self.node_requesters[ident] = []
 
        # Fill node_requesters:
        for r in rules:
            for node in r.depends_on:
                self.node_requesters[node.get_id()].append(r.target.get_id())

    def _build_aux(self, target: Node, target_stack: list[str]) -> None:
        if isinstance(target, StaticNode):
            target.verify_exists()
        else:
            assert isinstance(target, DynamicNode)
            ident = target.get_id()
            # Check for circular dependency:
            if ident in target_stack:
                raise RuntimeError(f"Found a circular dependency chain: " + ', '.join(target_stack + [ident]))
            
            # Find rule for creating this node:
            if not ident in self.rules.keys():
                raise RuntimeError(f"There is no rule for creating the dynamic node '{ident}'")
            rule: Rule = self.rules[ident]
            
            # Ensure all dependencies are up to date:
            for dep in rule.depends_on:
                self._build_aux(dep, target_stack + [ident])

            if not rule.is_up_to_date():
                rule.execute()

    def build(self, target: DynamicNode) -> None:
        self._build_aux(target, [])