"""Rewrite the auto_director_plan.json with proper story arc including Maya and Owen."""
import json
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with open('output/auto_director_plan.json') as f:
    plan = json.load(f)

scenes = plan['scenes']
style_prefix = plan.get('style_bible', {}).get('global_style', 'shallow depth of field, golden hour, cinematic, emotional, warm')

# Package IDs
maya_char_pkg = 'pkg_char_67dab2d7'
owen_char_pkg = 'pkg_char_0a0b6a7c'
buddy_char_pkg = 'pkg_char_c852b9c5'

# STORY ARC:
# Beat 0 (intro 0-6s): Buddy alone, entering park — keep
# Beat 1 (verse 6-12s): Wandering fountain — keep
# Beat 2 (chorus 12-18s): REWRITE — Maya spots Buddy, playful chase
# Beat 3 (verse 18-24s): REWRITE — Buddy searching, spots Owen
# Beat 4 (outro 24-30s): REWRITE — Reunion with Owen on bench

rewrites = {
    # Beat 2 — Maya enters the story
    8: {
        'action': 'A wide shot of the busy crowd lawn. The golden retriever weaves through groups of people on blankets. A small girl in a denim dress with pigtails notices the dog and points excitedly.',
        'prompt': f'{style_prefix}. Wide shot of autumn park crowd lawn. A golden retriever with red collar weaves through groups of people on blankets. A small girl in denim dress with brown pigtails spots the dog and points excitedly. Warm golden hour light, fallen leaves.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
    },
    9: {
        'action': 'Medium shot of Maya crouching down, reaching hands toward the golden retriever. The dog tilts its head curiously but stays just out of reach.',
        'prompt': f'{style_prefix}. Medium shot of a small girl in denim dress with brown pigtails crouching down, reaching hands toward a golden retriever with red collar. Dog tilts head curiously, stays out of reach. Autumn park, golden light.',
        'character_package_id': maya_char_pkg,
        'characterName': 'Maya',
    },
    10: {
        'action': 'The golden retriever playfully bounds away from Maya, looking back over its shoulder with tongue out. Maya laughs and gives chase across the autumn leaves.',
        'prompt': f'{style_prefix}. A golden retriever with red collar playfully bounds away, looking back over shoulder with tongue out. A small girl in denim dress chases after it across scattered autumn leaves. Park lawn, golden hour.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
    },
    11: {
        'action': 'Close-up of Maya stopping, hands on knees, catching her breath. She watches the dog trot away into the trees with concern.',
        'prompt': f'{style_prefix}. Close-up of a small girl in denim dress with brown pigtails, hands on knees, catching breath in autumn park. Watches a golden retriever trot away into golden-lit trees. Expression shifting from laughter to concern.',
        'character_package_id': maya_char_pkg,
        'characterName': 'Maya',
    },
    12: {
        'action': 'Insert shot of fallen autumn leaves swirling in golden light where Buddy just ran through.',
        'prompt': f'{style_prefix}. Close-up of fallen autumn leaves swirling in golden hour light on a park path. A red maple leaf drifts down slowly. Warm, nostalgic mood. No people.',
        'character_package_id': None,
        'characterName': None,
    },
    # Beat 3 — Buddy searching, spots Owen
    13: {
        'action': 'Medium-wide shot of the golden retriever trotting along a wooded path, nose to ground, searching. Deep amber light, long shadows.',
        'prompt': f'{style_prefix}. Medium-wide shot of a golden retriever with red collar trotting along wooded park path, nose to ground, searching. Deep amber golden hour light, long shadows on autumn leaves.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
    },
    14: {
        'action': 'Close-up of the golden retriever suddenly lifting its head, ears perking forward sharply. Something familiar caught its attention.',
        'prompt': f'{style_prefix}. Close-up of a golden retriever with red collar lifting its head suddenly, ears perking sharply forward, nose twitching with recognition. Bright brown eyes focused on distance. Deep golden light on fur.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
    },
    15: {
        'action': 'POV from dog eye level. At the far end of a tree-lined path, a man sits alone on a park bench reading a book in the last golden rays.',
        'prompt': f'{style_prefix}. Low angle POV from dog eye level down a tree-lined autumn park path. At far end, a man in autumn jacket sits alone on wooden park bench reading a book, bathed in golden sunset rays through trees.',
        'character_package_id': owen_char_pkg,
        'characterName': 'Owen',
        'environment_package_id': 'pkg_envi_82c911b9',
    },
    16: {
        'action': 'Wide shot of the golden retriever breaking into a joyful run down the path toward the distant bench. Leaves kick up behind its paws.',
        'prompt': f'{style_prefix}. Wide shot of a golden retriever with red collar breaking into joyful full sprint down tree-lined autumn park path. Leaves kick up behind paws. Man on distant bench looks up. Magical deep golden sunset.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
        'environment_package_id': 'pkg_envi_82c911b9',
    },
    # Beat 4 — Reunion
    17: {
        'action': 'Medium shot of Owen on the bench. He looks up from his book, eyes widening with recognition and overwhelming relief as Buddy sprints toward him.',
        'prompt': f'{style_prefix}. Medium shot of a man in autumn jacket on wooden park bench. Looks up from paperback, eyes widening with recognition and joy. Golden sunset on face, autumn trees behind.',
        'character_package_id': owen_char_pkg,
        'characterName': 'Owen',
        'environment_package_id': 'pkg_envi_82c911b9',
    },
    18: {
        'action': 'The golden retriever leaps into Owen arms. Owen kneels, wraps arms around Buddy. The dog licks his face, tail wagging wildly.',
        'prompt': f'{style_prefix}. A golden retriever with red collar leaps joyfully into a mans arms by a park bench. Man kneels, wrapping arms around dog, face buried in golden fur. Dog licks face, tail blurred with wagging. Golden sunset, autumn leaves falling.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
        'environment_package_id': 'pkg_envi_82c911b9',
    },
    19: {
        'action': 'Final close-up. Buddy head rests on Owen shoulder, eyes closing contentedly. Owen hand strokes the golden fur. Pure joy and relief.',
        'prompt': f'{style_prefix}. Intimate close-up of a golden retriever with red collar resting head on mans shoulder, eyes closing contentedly. Mans hand strokes golden fur. Pure joy and relief. Warm golden sunset glow, soft focus autumn park.',
        'character_package_id': buddy_char_pkg,
        'characterName': 'Buddy',
        'environment_package_id': 'pkg_envi_82c911b9',
    },
}

# Apply rewrites
for idx, updates in rewrites.items():
    s = scenes[idx]
    for key, val in updates.items():
        if val is not None:
            s[key] = val
        elif key in s:
            s[key] = None

# Clear ALL clips for full regeneration with new @Tag pipeline
for s in scenes:
    s['clip_path'] = ''
    s['has_clip'] = False
    s['status'] = 'pending'
    s['gen_hash'] = ''
    s['first_frame_path'] = ''

plan['status'] = 'ready'
plan['blocking_errors'] = []

with open('output/auto_director_plan.json', 'w') as f:
    json.dump(plan, f, indent=2)

print('Plan rewritten!')
print(f'Total scenes: {len(scenes)}')
print(f'Shots rewritten: {len(rewrites)}')
print(f'All {len(scenes)} clips cleared for regeneration')
print()
for i, s in enumerate(scenes):
    char = s.get('characterName') or 'env'
    print(f'  {i:2d} {s["shot_id"]:8s} | {char:>8s} | {s["action"][:70]}')
