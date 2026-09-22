"""公开前脱敏脚本：用户名路径→通用占位；license 细节→合规中性描述。

原文件备份到 var/public-scrub-backup/（该目录被 gitignore 排除）。
幂等：重复运行无新改动时 changed 为空。
"""
import io
import os
import re
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP = os.path.join(REPO, 'var', 'public-scrub-backup')

def _pairs():
    """字面量对（from_str, to_str）——不用正则，直接 str.replace。"""
    P = [
        # 用户路径 → 通用占位（顺序敏感：长的先替换）
        (r'<VENV_DSH_SIM>\Scripts\python.exe', '<VENV_DSH_SIM>\\Scripts\\python.exe'),
        (r'<PYTHON_ENV>\Scripts\python.exe', '<PYTHON_ENV>\\Scripts\\python.exe'),
        ('C:\\\\Users\\\\Kogami\\\\.workbuddy\\\\binaries\\\\python\\\\envs\\\\dsh-sim\\\\Scripts\\\\python.exe', '<VENV_DSH_SIM>\\\\Scripts\\\\python.exe'),
        ('C:\\\\Users\\\\Kogami\\\\.workbuddy\\\\binaries\\\\python\\\\envs\\\\default\\\\Scripts\\\\python.exe', '<PYTHON_ENV>\\\\Scripts\\\\python.exe'),
        ('C:\\\\Users\\\\Kogami\\\\AppData\\\\Local\\\\Temp', '<TEMP>'),
        ('<TEMP>', '<TEMP>'),
        ('C:\\\\Users\\\\Kogami\\\\license.dat', '<LICENSE_FILE>'),
        (r'<LICENSE_FILE>', '<LICENSE_FILE>'),
        ('D:\\\\StarCCM Codebuddy', '<STARCCM_CLI_DIR>'),
        (r'<STARCCM_CLI_DIR>', '<STARCCM_CLI_DIR>'),
        ('D:\\\\dsh', '<DSH_HOME>'),
        (r'<DSH_HOME>', '<DSH_HOME>'),
        ('C:\\\\Users\\\\Kogami\\\\WorkBuddy\\\\dsh-windows\\\\dsh-sim', '<REPO_ROOT>'),
        (r'<REPO_ROOT>', '<REPO_ROOT>'),
        ('C:\\\\Users\\\\Kogami', '<USER_HOME>'),
        (r'<USER_HOME>', '<USER_HOME>'),
        (r'<STARCCM_2402_BAT>', '<STARCCM_2402_BAT>'),
        (r'<STARCCM_1706_BAT>', '<STARCCM_1706_BAT>'),
        # license 合规敏感句 → 中性
        ('license 合规状态待组织内部确认（TBD-07）',
         'license 合规状态待组织内部确认（TBD-07）'),
        ('ccmpsuite checked out（授权文件路径已脱敏）', 'ccmpsuite checked out（授权文件路径已脱敏）'),
        ('ccmpsuite 可 checkout；ccmpsuite_init 缺失', 'ccmpsuite 可 checkout；ccmpsuite_init 缺失'),
        ('<LICENSE_FILE>（路径已脱敏），ccmpsuite 可 checkout', '<LICENSE_FILE>（路径已脱敏），ccmpsuite 可 checkout'),
    ]
    return P


def main() -> None:
    os.makedirs(BACKUP, exist_ok=True)
    targets = []
    for root, dirs, files in os.walk(REPO):
        rel = os.path.relpath(root, REPO)
        parts = rel.split(os.sep)
        dirs[:] = [d for d in dirs if d not in ('var', '__pycache__', '.git', '.pytest_cache', 'node_modules', '.venv')]
        for f in files:
            if f.endswith(('.py', '.json', '.md', '.yml', '.yaml', '.js', '.ps1', '.html')):
                targets.append(os.path.join(root, f))

    changed = []
    pairs = _pairs()
    for p in targets:
        src = io.open(p, encoding='utf-8').read()
        new = src
        for frm, to in pairs:
            new = new.replace(frm, to)
        if new != src:
            bak_name = os.path.relpath(p, REPO).replace(os.sep, '__')
            shutil.copy2(p, os.path.join(BACKUP, bak_name))
            io.open(p, 'w', encoding='utf-8', newline='').write(new)
            changed.append(os.path.relpath(p, REPO))

    print(f'脱敏文件 {len(changed)} 个：')
    for p in changed:
        print(' ', p)


if __name__ == '__main__':
    main()
