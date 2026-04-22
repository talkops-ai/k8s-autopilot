import os, re

skills_dir = 'skills/helm-operator'

for skill_name in sorted(os.listdir(skills_dir)):
    skill_path = os.path.join(skills_dir, skill_name, 'SKILL.md')
    if not os.path.isfile(skill_path):
        continue

    content = open(skill_path).read()

    # Parse frontmatter
    fm_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    fm = fm_match.group(1) if fm_match else ''

    # 1. Name field
    name_match = re.search(r'^name:\s*(.+)$', fm, re.MULTILINE)
    name = name_match.group(1).strip() if name_match else ''
    name_valid = bool(re.match(r'^[a-z][a-z0-9-]{0,62}[a-z0-9]$', name)) and '--' not in name
    name_matches_dir = name == skill_name

    # 2. Description
    desc_match = re.search(r'^description:\s*>-?\s*\n(.*?)(?=\n\w|\n---)', fm, re.DOTALL)
    if not desc_match:
        desc_match = re.search(r'^description:\s*(.+)$', fm, re.MULTILINE)
    desc = desc_match.group(1).strip().replace('\n  ', ' ').replace('\n', ' ') if desc_match else ''
    desc_len = len(desc)
    desc_ok = 1 <= desc_len <= 1024
    desc_use_when = 'use when' in desc.lower() or 'trigger' in desc.lower()

    # 3. Compatibility
    has_compat = 'compatibility:' in fm

    # 4. Allowed-tools
    has_tools = 'allowed-tools:' in fm

    # 5. Body checks
    body_start = content.find('---', content.find('---') + 3) + 3
    body = content[body_start:]

    has_when_to_use = '## when to use' in body.lower()
    has_safety = 'safety rules' in body.lower()
    has_gotchas = '## gotchas' in body.lower()
    has_response = '## response format' in body.lower()
    has_validation = 'validat' in body.lower() and ('step' in body.lower() or 'loop' in body.lower())
    has_progressive = '| reference' in body.lower() or 'reference file' in body.lower()

    # Score
    checks = [
        name_valid and name_matches_dir,
        desc_ok and desc_use_when,
        has_compat,
        has_tools,
        has_when_to_use,
        has_safety,
        has_gotchas,
        has_response,
        has_validation,
        has_progressive,
    ]
    score = sum(checks)

    s = lambda b: 'Y' if b else 'N'
    print(f"{skill_name} ({score}/10):")
    print(f"  name={s(name_valid and name_matches_dir)} desc={s(desc_ok and desc_use_when)}({desc_len}c) compat={s(has_compat)} tools={s(has_tools)}")
    print(f"  when={s(has_when_to_use)} safety={s(has_safety)} gotchas={s(has_gotchas)} response={s(has_response)} validation={s(has_validation)} disclosure={s(has_progressive)}")
    print()
