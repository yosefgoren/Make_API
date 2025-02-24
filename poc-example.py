#!/home/yogo/env/bin/python3
from makeapi import *
import click

rules: list[CreationRule] = []

poc_example_src = StaticFileNode("poc-example.c")
modified_poc_example_src = FileModificationNode(poc_example_src, "poc-example-mod-1")
# = FileModificationNode()

obj1 = CreatedFileNode("example-dep.o")
rules += [CompileRule(obj1, [StaticFileNode("example-dep.c")], flags=["-c"])]

obj2 = CreatedFileNode("poc-example.o")
rules += [ShellFileModifyRule(modified_poc_example_src, [], f"echo '\n//stuff\n' >> poc-example.c")]
rules += [CompileRule(obj2, [poc_example_src], [modified_poc_example_src], flags=["-c"])]

tgt = CreatedFileNode("poc-example-exec")
rules.append(CompileRule(tgt, [obj1, obj2]))
bs = BuildSystem(rules)


@click.group()
def cli():
    pass

@cli.command("build")
def build():
    print(f"Building target...")
    bs.build(tgt)

@cli.command("clean")
def clean():
    print(f"Cleaning target...")
    bs.clean(tgt)

@cli.command("dag")
def dag():
    print(f"Printing DAG...")
    bs.dag(tgt)

if __name__ == "__main__":
    cli()