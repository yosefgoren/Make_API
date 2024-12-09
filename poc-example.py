from makeapi import *

def gcc_cmd(rule: Rule) -> str:
    sources = " ".join([dep.get_id() for dep in rule.depends_on])
    return f"gcc {sources} -o {rule.target.get_id()}"

target = DynamicFileNode("poc-example-exec")
rule = ShellRule(target, [StaticFileNode("poc-example.c")], gcc_cmd)
BuildSystem([rule]).build(target)