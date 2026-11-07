"""维护者本机 CLI：检查工具契约、初始化状态、启用、停用及软移除。"""
import argparse
import json

from .contract import APIError
from .registry import ToolRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-file", help="服务 serve --tool-registry-file 使用的同一个状态文件")
    parser.add_argument("--api-version", choices=["v1", "v2"], default="v1")
    parser.add_argument("--examples", action="store_true", help="v2 本机模拟工具，须与运行服务的 --v2-examples 保持一致")
    parser.add_argument("action", choices=["init", "list", "show", "enable", "disable", "remove", "restore"])
    parser.add_argument("tool_id", nargs="?")
    args = parser.parse_args()
    if args.examples and args.api_version != "v2":
        parser.error("--examples 仅用于 v2")
    if args.api_version == "v2":
        from .v2_adapters import build_registry
        registry = build_registry(state_file=args.registry_file, examples=args.examples)
    else:
        registry = ToolRegistry(args.registry_file)
    try:
        if args.action in {"init", "list"} and args.tool_id is not None:
            parser.error("init 和 list 不接受 tool_id")
        if args.action not in {"init", "list"} and args.tool_id is None:
            parser.error("该操作需要 tool_id")
        if args.action == "init":
            registry.initialize()
            result = {"initialized": True, "runtime_probed": False}
        elif args.action == "list":
            result = {"runtime_probed": False, "tools": registry.management_status()}
        elif args.action == "show":
            result = {**registry.contract(args.tool_id, "gpu"), "runtime_probed": False}
        else:
            result = registry.change(args.tool_id, args.action)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, APIError) as exc:
        parser.exit(2, (exc.message if isinstance(exc, APIError) else str(exc)) + "\n")


if __name__ == "__main__":
    main()
