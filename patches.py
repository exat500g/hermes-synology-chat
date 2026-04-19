#!/usr/bin/env python3
"""
Synology Chat Adapter — 补丁应用脚本

用法:
  python3 patches.py --hermes-dir ~/.hermes/hermes-agent

将自动应用以下修改:
  1. gateway/config.py    — 添加 SYNOLOGY_CHAT 枚举值
  2. gateway/run.py       — 添加适配器工厂 + 用户授权
  3. hermes_cli/platforms.py — 注册平台到 CLI
  4. 复制 synology_chat.py 到 gateway/platforms/
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


def apply_config_patch(hermes_dir: Path) -> bool:
    """gateway/config.py — 在 Platform 枚举中添加 SYNOLOGY_CHAT"""
    config_path = hermes_dir / "gateway" / "config.py"
    if not config_path.exists():
        print(f"  ❌ {config_path} not found")
        return False

    content = config_path.read_text()
    if "SYNOLOGY_CHAT" in content:
        print("  ✅ gateway/config.py — already patched")
        return True

    # Find the QQBOT line and add SYNOLOGY_CHAT after it
    # Pattern: QQBOT = "qqbot"  (with possible trailing whitespace/newline)
    pattern = r'(QQBOT\s*=\s*"qqbot")'
    match = re.search(pattern, content)
    if not match:
        print("  ❌ Could not find QQBOT enum entry in config.py")
        return False

    insert_pos = match.end()
    new_line = '\n    SYNOLOGY_CHAT = "synology_chat"'
    new_content = content[:insert_pos] + new_line + content[insert_pos:]
    config_path.write_text(new_content)
    print("  ✅ gateway/config.py — added SYNOLOGY_CHAT enum")
    return True


def apply_run_patch(hermes_dir: Path) -> bool:
    """gateway/run.py — 添加适配器工厂 + 用户授权映射"""
    run_path = hermes_dir / "gateway" / "run.py"
    if not run_path.exists():
        print(f"  ❌ {run_path} not found")
        return False

    content = run_path.read_text()
    if "SYNOLOGY_CHAT" in content:
        print("  ✅ gateway/run.py — already patched")
        return True

    patched = False
    new_content = content

    # 1. Add SYNOLOGY_CHAT_ALLOWED_USERS to allowlist check
    if '"SYNOLOGY_CHAT_ALLOWED_USERS"' not in content:
        # Find "QQ_ALLOWED_USERS" and add SYNOLOGY after it
        pattern = r'("QQ_ALLOWED_USERS",?\s*\n)'
        match = re.search(pattern, content)
        if match:
            insert = match.group(1) + '                       "SYNOLOGY_CHAT_ALLOWED_USERS",\n'
            new_content = new_content.replace(match.group(0), insert, 1)
            patched = True

    # 2. Add SYNOLOGY_CHAT_ALLOW_ALL_USERS to allow-all check
    if '"SYNOLOGY_CHAT_ALLOW_ALL_USERS"' not in new_content:
        # Use context to disambiguate: _allow_all uses "or any(" prefix
        pattern = r'("QQ_ALLOW_ALL_USERS",?\s*\n\s*\))'
        match = re.search(pattern, new_content)
        if match:
            # Insert before the closing paren
            insert = '                       "SYNOLOGY_CHAT_ALLOW_ALL_USERS",\n' + match.group(0)
            new_content = new_content[:match.start()] + insert + new_content[match.end():]
            patched = True

    # 3. Add adapter factory in _create_adapter
    adapter_block = '''
        elif platform == Platform.SYNOLOGY_CHAT:
            from gateway.platforms.synology_chat import SynologyChatAdapter, check_synology_chat_requirements
            if not check_synology_chat_requirements():
                logger.warning("Synology Chat: aiohttp not installed. Run: pip install aiohttp")
                return None
            return SynologyChatAdapter(config)
'''
    # Find "return None" after the QQ adapter block
    if 'return QQAdapter(config)' in new_content:
        pattern = r'(return QQAdapter\(config\)\s*\n)'
        match = re.search(pattern, new_content)
        if match:
            new_content = new_content.replace(match.group(0), match.group(0) + adapter_block, 1)
            patched = True

    # 4. Add to platform_allowlist_map
    if 'Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOWED_USERS"' not in new_content:
        pattern = r'(Platform\.QQBOT:\s*"QQ_ALLOWED_USERS",?\s*\n)(?!.*SYNOLOGY_CHAT)'
        match = re.search(pattern, new_content)
        if match:
            insert = match.group(0) + '            Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOWED_USERS",\n'
            new_content = new_content.replace(match.group(0), insert, 1)
            patched = True

    # 5. Add to platform_allow_all_map
    if 'Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOW_ALL_USERS"' not in new_content:
        pattern = r'(Platform\.QQBOT:\s*"QQ_ALLOW_ALL_USERS",?\s*\n)(?!.*SYNOLOGY_CHAT)'
        match = re.search(pattern, new_content)
        if match:
            insert = match.group(0) + '            Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOW_ALL_USERS",\n'
            new_content = new_content.replace(match.group(0), insert, 1)
            patched = True

    if patched:
        run_path.write_text(new_content)
        print("  ✅ gateway/run.py — added adapter factory + auth mappings")
    else:
        print("  ⚠️  gateway/run.py — no changes applied (check manually)")
    return True


def apply_platforms_patch(hermes_dir: Path) -> bool:
    """hermes_cli/platforms.py — 注册平台到 CLI"""
    platforms_path = hermes_dir / "hermes_cli" / "platforms.py"
    if not platforms_path.exists():
        print(f"  ❌ {platforms_path} not found")
        return False

    content = platforms_path.read_text()
    if "synology_chat" in content:
        print("  ✅ hermes_cli/platforms.py — already patched")
        return True

    # Find the qqbot line and add synology_chat after it
    pattern = r'\("qqbot".*?\),?\s*\n'
    match = re.search(pattern, content)
    if not match:
        print("  ❌ Could not find qqbot entry in platforms.py")
        return False

    new_line = '    ("synology_chat",  PlatformInfo(label="🏠 Synology Chat",   default_toolset="hermes-synology-chat")),\n'
    new_content = content[:match.end()] + new_line + content[match.end():]
    platforms_path.write_text(new_content)
    print("  ✅ hermes_cli/platforms.py — registered synology_chat")
    return True


def copy_adapter(hermes_dir: Path, script_dir: Path) -> bool:
    """复制 synology_chat.py 到 gateway/platforms/"""
    src = script_dir / "synology_chat.py"
    dst = hermes_dir / "gateway" / "platforms" / "synology_chat.py"
    if not src.exists():
        print(f"  ❌ {src} not found")
        return False

    shutil.copy2(src, dst)
    print(f"  ✅ Copied synology_chat.py → {dst}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Apply Synology Chat adapter patches to Hermes Agent")
    parser.add_argument("--hermes-dir", default=os.path.expanduser("~/.hermes/hermes-agent"),
                        help="Path to hermes-agent directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    hermes_dir = Path(args.hermes_dir)
    script_dir = Path(__file__).parent

    print(f"Hermes Agent: {hermes_dir}")
    print(f"Adapter source: {script_dir}")
    print()

    if args.dry_run:
        print("(dry run — no changes will be made)")
        return

    results = []
    results.append(("config.py", apply_config_patch(hermes_dir)))
    results.append(("run.py", apply_run_patch(hermes_dir)))
    results.append(("platforms.py", apply_platforms_patch(hermes_dir)))
    results.append(("synology_chat.py", copy_adapter(hermes_dir, script_dir)))

    print()
    print("=" * 50)
    all_ok = all(ok for _, ok in results)
    if all_ok:
        print("✅ All patches applied successfully!")
        print("\nNext steps:")
        print("  1. pip install aiohttp  (if not installed)")
        print("  2. Configure ~/.hermes/config.yaml (see README.md)")
        print("  3. hermes gateway restart")
    else:
        print("⚠️  Some patches failed — check output above")
        sys.exit(1)


if __name__ == "__main__":
    main()
