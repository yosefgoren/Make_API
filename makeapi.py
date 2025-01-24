from typing import Callable
from abc import ABC, abstractmethod
import os
from dataclasses import dataclass
from enum import Enum
import json
import atexit
import shutil
import filecmp
import hashlib

DATABASE_FILENAME = "makeapi_database.json"
class Database:
    """
    This database is internal to the implementation of make API, and i used to store persistant information
    regarding the states of the objects managed by the build system
    """
    def __init__(self):
        if not os.path.exists(DATABASE_FILENAME):
            self.data = dict()
        else:
            self.data = json.load(open(DATABASE_FILENAME, 'r'))
        atexit.register(self.sync) # This ensures sync will be called if the program is interrupted or exits normally

    def sync(self) -> None:
        json.dump(self.data, open(DATABASE_FILENAME, 'w'), indent=4)

    def clean(self) -> None:
        if os.path.exists(DATABASE_FILENAME):
            os.remove(DATABASE_FILENAME)
        atexit.unregister(self.sync)

DATABASE: None | Database = None
def get_db() -> Database:
    global DATABASE
    if DATABASE is None:
        DATABASE = Database()
    return DATABASE

class BuildState(Enum):
    CLEAN = "clean"
    DIRTY = "dirty" 
    BUILT = "built" 

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

@dataclass
class FileModificationNode(DynamicNode):
    """
    This node does not represent the file itself,
    instead it represents a specific modification made to a file.

    One modification to the file may invalidate a previous modification to it.
    """

    modified_file: FileNode
    modification_key: str

    def get_id(self):
        return f"{self.modified_file.get_id()}_{self.modification_key}"

    def get_time(self) -> float | None:
        path: str | None = self.get_clone_file_path()
        if path is None:
            return None
        try:
            return os.path.getmtime(path)
        except FileNotFoundError:
            return None

    def _get_clone_paths(self) -> dict[str, str]:
        """
        Returns a reference to the clone paths dictionary from the persistant global database.
        This is used to store paths of clones of files which are made before modifying them.
        """
        #TODO: verify 'clone_paths' is synced back to database with current code.
        db = get_db().data
        if "clone_paths" not in db.keys():
            db["clone_paths"] = dict()
        return db["clone_paths"] # typing: ignore

    def clean(self) -> None:
        path: str | None = self.get_clone_file_path()

        # Verify not requested to clean a file which does not exist:
        if path is None:
            # print(f"Warning: Requested to clean the modification to '{self.modified_file.path}' but a clone file was not found.")
            return
        
        shutil.move(path, self.modified_file.path)
        
        # Cleanup in the database:
        clone_paths: dict[str, str] = self._get_clone_paths()
        del clone_paths[self.modified_file.get_id()]

    def create_clone_file(self) -> None:
        clone_paths: dict[str, str] = self._get_clone_paths()
        head, tail = os.path.split(self.modified_file.path)
        new_clone_path = os.path.join(head, f"__clone__{tail}")
        
        # Verify not requesting to clone a file which already has a clone:
        assert self.modified_file.get_id() not in clone_paths.keys()
        if os.path.exists(new_clone_path):
            print(f"Warning: clone file '{new_clone_path}' already exists. Overriding.")

        shutil.copy(self.modified_file.path, new_clone_path)
        clone_paths[self.modified_file.get_id()] = new_clone_path

    def get_clone_file_path(self) -> str | None:
        clone_paths: dict[str, str] = self._get_clone_paths()
        
        # The ID used in the 'clone_paths' dict is that of the modified file, not of the modification.
        # This makes more sense since theoretically - a single modification might modify multiple files, and different modifications may target the same file.
        
        if self.modified_file.get_id() not in clone_paths.keys():
            return None
        else:
            assert os.path.exists(clone_paths[self.modified_file.get_id()])
            return clone_paths[self.modified_file.get_id()]

class CreatedFileNode(DynamicNode, FileNode):
    def clean(self) -> None:
        if os.path.exists(self.path):
            print(f"Removing file: '{self.path}'")
            os.remove(self.path)

class StaticFileNode(StaticNode, FileNode):
    def _check_exists(self) -> str | None:
        if not os.path.exists(self.path):
            return "File not found"
        return None

@dataclass
class Rule(ABC): #TODO: Maybe change implementation of generic rule to not implement 'is_up_to_date' and use current impl in 'CreateRule'
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

class ModifyRule(Rule):
    """
    A modify rule builds a dynamic node by making a modification to it from it's clean state.
    From the perspective of the modify rule, the target node can be in 3 states:
    1. Clean: This is the base state in which the target will be found after invoking it's 'clean' method.
    2. Built: This is the state in which the object can be found after being successfuly modified from the clean state.
    3. Dirty: This is any state other than the clean or built states.
    
    When requested to execute, a modify rule will check if the target is built - and if so - do nothing.
    Otherwise, if the target is dirty - it will clean the target.
    Next, the modification will be executed.
    """

    @abstractmethod
    def _get_build_state(self) -> BuildState:
        pass

    @abstractmethod
    def _do_modification(self) -> None:
        pass

    def execute(self) -> None:
        state = self._get_build_state()
        if state is BuildState.BUILT:
            return
        if state is BuildState.DIRTY:
            self.target.clean()
        self._do_modification()

def get_md5sum(path: str) -> str:
    return hashlib.md5(open(path, 'rb').read()).hexdigest()

class FileModifyRule(ModifyRule):
    def __init__(self, target: FileModificationNode, depends_on: list[Node]):
        super().__init__(target, depends_on)
        self.target: FileModificationNode = target

    def _get_modified_hashes(self) -> dict[str, str]:
        db = get_db().data
        if "modified_hashes" not in db.keys():
            db["modified_hashes"] = dict()
        return db["modified_hashes"] # typing: ignore

    def _get_build_state(self) -> BuildState:
        # If file hash matches the hash stored after modification - the target is built:
        actual_hash: str = get_md5sum(self.target.modified_file.path)
        modified_hashes: dict[str, str] = self._get_modified_hashes()
        
        if self.target.get_id() not in modified_hashes:
            return BuildState.CLEAN
        
        saved_hash: str = modified_hashes[self.target.get_id()]
        if actual_hash == saved_hash:
            return BuildState.BUILT

        # If the clone does not exist or file matches it's clone - we are in the clean state:
        clone_path: str | None = self.target.get_clone_file_path()
        if clone_path is None or filecmp.cmp(self.target.modified_file.path, clone_path, shallow=False):
            return BuildState.CLEAN
        
        # Otherwise - we are in the dirty state:
        return BuildState.DIRTY
    
    def _do_modification(self) -> None:
        # Ensure the target is in a clean state:
        assert self._get_build_state() == BuildState.CLEAN

        # Create a clone if it does not exist:
        if self.target.get_clone_file_path() is None:
            self.target.create_clone_file()

        # Apply the actual modification:
        self._file_modification()

        # Update the modified hashes database:
        modified_hashes: dict[str, str] = self._get_modified_hashes()
        modified_hashes[self.target.get_id()] = get_md5sum(self.target.modified_file.path)

    @abstractmethod
    def _file_modification(self):
        """
        This is the only method that should be overriden by implementations of this class.
        It should include only the core modification of the target file, excluding any management of the modifications such as cloning or restoring the file.
        """
        pass

class ShellFileModifyRule(FileModifyRule):
    """
    A rule targeting a modification node, which modifies a file by running a shell command.
    """
    def __init__(self, target: FileModificationNode, depends_on: list[Node], modification_cmd: str):
        """
        modification_cmd: The shell command line which will apply the wanted modification to the file targeted by the modification.
        """
        super().__init__(target, depends_on)
        self.modification_cmd = modification_cmd

    def _file_modification(self):
        os.system(self.modification_cmd)

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
            target: CreatedFileNode,
            source_files: list[FileNode],
            other_dependencies: list[Node] = [],
            compiler: str = "cc",
            flags: list[str] = []
        ):
        super().__init__(
            target,
            list(source_files+other_dependencies), # For stupid mypy linter
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

        get_db().clean()
        
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
    