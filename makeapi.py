from typing import Callable
from abc import ABC, abstractmethod
import os
from dataclasses import dataclass

class Node(ABC):
    @abstractmethod
    def get_id(self) -> str:
        """A unique identifier used to distinguish this node from others"""
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
    
    @abstractmethod
    def clean(self) -> None:
        """Clean any resource associated with this dynamic node"""
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
    def clean(self) -> None:
        if os.path.exists(self.path):
            print(f"Removing file: '{self.path}'")
            os.remove(self.path)

class StaticFileNode(StaticNode, FileNode):
    def _check_exists(self) -> str | None:
        if not os.path.exists(self.path):
            return "File not found"
        return None

# @dataclass
# class JsonEntryNode(DynamicNode):
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
    cmd: str

    def __init__(self, target: DynamicNode, deps: list[Node], cmd: str):
        super().__init__(target, deps)
        self.cmd = cmd

    def execute(self) -> None:
        print(self.cmd)
        if os.system(self.cmd) != 0:
            raise RuntimeError(f"Command '{self.cmd}' failed, while building target '{self.target.get_id()}'")

class CompileRule(ShellRule):
    def __init__(
            self,
            target: DynamicFileNode,
            source_files: list[FileNode],
            header_files: list[FileNode] = [],
            compiler: str = "cc",
            flags: list[str] = []
        ):
        super().__init__(
            target,
            list(source_files+header_files), # For stupid mypy linter
            f"{compiler} {' '.join(flags)} {' '.join([n.path for n in source_files])} -o {target.path}"
        )

@dataclass
class BuildSystem:
    def __init__(self, rules: list[Rule], skip_verification: bool = False):
        """
        The set of nodes is infered from the rules.

        Normally the following static properties of the build system are verified:
            1. All static nodes exist.
            2. Each dynamic node has a rule for creating it.
            3. There are no dependency loops.
        This can be disabled by setting 'skip_verification'.
        """
        
        self.rules: dict[str, Rule] = dict()
        self.nodes: dict[str, Node] = dict()

        # NOTE: node_requesters is currently dead code, but when adding multiprocessing in the future,
        #    node_requesters will be needed to notify that it's requester nodes might be ready to be created.
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

        # Perform static verification, if needed
        if not skip_verification:
            self._run_static_checks()

    def build(self, target: DynamicNode | None = None) -> None:
        start_nodes: list[Node] = self._all_or_one(target)

        def execute_rule(node: Node) -> None:
            if isinstance(node, DynamicNode):
                rule: Rule = self._find_rule(node)
                if not rule.is_up_to_date():
                    rule.execute()

        self.traverse_dag(start_nodes, postorder_action=execute_rule)
        
    def clean(self, target: DynamicNode | None = None) -> None:
        """Specify None to clean all dynamic nodes, or a target to clean it and anything it (recursively) depends on."""
        
        def clean_node(node: Node) -> None:
            if isinstance(node, DynamicNode):
                node.clean()
        self.traverse_dag(self._all_or_one(target), postorder_action=clean_node)
        
    def dag(self, target: Node | None = None) -> None:
        """Print the nodes/dependencies DAG"""
        depth: int = 0
        def preorder(node: Node) -> None:
            nonlocal depth
            print("+--"*depth + node.get_id())
            depth += 1
        def postorder(node: Node) -> None:
            nonlocal depth
            depth -= 1
        self.traverse_dag(self._all_or_one(target), preorder, postorder)

    def traverse_dag(
        self,
        starting_nodes: list[Node],
        preorder_action: Callable[[Node], None] = lambda _: None,
        postorder_action: Callable[[Node], None] = lambda _: None,
    ) -> set[str]:
        """
        This function efficiently(1) implements ordered(2) traversal through the dependency DAG(3) while verifying no circular dependencies exist.

        (1) efficiently means it avoids going over the same nodes more than once. This algorithm would go thourgh pascal's triangle in polynomial time.
        (2) postorder_action is executed on the parent node after being executed on all of it's sons, preorder is before.
        (3) DAG: Directed Acyclic Graph, A graph without loops.

        A set of traversed nodes is returned.
        """
        traversed_nodes: set[str] = set()
        for start in starting_nodes:
            if start.get_id() not in traversed_nodes:
                self._traverse_dag_aux(start, set(), traversed_nodes, preorder_action, postorder_action)
        return traversed_nodes
    
    # 'Private' methods: ======================================

    def _run_static_checks(self) -> None:
        """See documentation in __init__"""
        for node in self.nodes.values():
            if isinstance(node, StaticNode):
                # 1.
                node.verify_exists()
            else:
                assert isinstance(node, DynamicNode)
                # 2.
                self._find_rule(node)
        # 3.
        self.traverse_dag(list(self.nodes.values()))

    def _find_rule(self, target: DynamicNode) -> Rule:
        ident = target.get_id()
        if not ident in self.rules.keys():
            raise RuntimeError(f"There is no rule for creating the dynamic node '{ident}'")
        return self.rules[ident]

    def _traverse_dag_aux(
        self,
        target: Node,
        node_stack: set[str],
        traversed_nodes: set[str],
        preorder_action: Callable[[Node], None],
        postorder_action: Callable[[Node], None],
    ) -> None:
        """
        'node_stack' is used to find circular dependency loops.
        'traversed_nodes' is used to avoid the addition of the same node multiple times.
        """
        tgt_id = target.get_id()
        traversed_nodes.add(tgt_id)

        preorder_action(target)
        if isinstance(target, DynamicNode):
            rule: Rule = self._find_rule(target)
            for dep in rule.depends_on:
                dep_id: str = dep.get_id()
                if dep_id in node_stack:
                    raise RuntimeError(f"Found a circular dependency chain: " + ', '.join(list(node_stack.union({dep_id}))))
                if dep_id not in traversed_nodes:
                    self._traverse_dag_aux(dep, node_stack.union({tgt_id}), traversed_nodes, preorder_action, postorder_action)
        postorder_action(target)

    def _build_aux(self, target: Node, target_stack: list[str]) -> None:
        if isinstance(target, StaticNode):
            target.verify_exists()
        else:
            assert isinstance(target, DynamicNode)
            ident = target.get_id()
            # Check for circular dependency:
            if ident in target_stack:
                raise RuntimeError(f"Found a circular dependency chain: " + ', '.join(target_stack + [ident]))
            
            rule: Rule = self._find_rule(target)
            
            # Ensure all dependencies are up to date:
            for dep in rule.depends_on:
                self._build_aux(dep, target_stack + [ident])

            if not rule.is_up_to_date():
                rule.execute()

    def _all_or_one(self, target: Node | None) -> list[Node]:
        """Returns all nodes if target is None, or just the target otherwise"""
        return list(self.nodes.values()) if target is None else [target]
    