#!/home/yogo/env/bin/python3
from makeapi import *
import click

rules: list[Rule] = []

src_names = [
    "poc-example",
    "example-dep"
]
objs = [DynamicFileNode(f"{name}.o") for name in src_names]
rules += [CompileRule(o, [StaticFileNode(f"{name}.c")], flags=["-c"]) for o, name in zip(objs, src_names)]

tgt = DynamicFileNode("poc-example-exec")
rules.append(CompileRule(tgt, objs))
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