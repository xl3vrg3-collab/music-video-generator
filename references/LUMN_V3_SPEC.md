# LUMN Studio V3 — Full Movie Production App Spec
_Saved 2026-04-02 from user requirements_

## Core Vision
AI film studio with one connected workflow:
1. Define project/world
2. Create or upload references  
3. Generate locked reusable assets
4. Build shots
5. Edit sequence/timeline
6. Add voice/music/transitions
7. Output full video

## Top-Level Workspaces
A. PROJECT / WORLD — creative rules, locks, style guide
B. ASSETS — characters, costumes, environments, props, voices, music
C. SHOTS — individual generation units with shot types
D. SEQUENCE / TIMELINE — assembly, transitions, reordering
E. AUDIO — voice, music, ambience, SFX
F. OUTPUT — render, export

## Layered Flow
1. Project/World → 2. Asset Creation → 3. Shot Generation → 4. Timeline Assembly → 5. Audio + Transitions → 6. Final Output

## Generation Modes
- Direct Video (wide/establishing/motion-first)
- Hero Still → Animate (close-ups/hero/face-critical)
- Still Only (concept/ideation/key art)

## Reference Priority by Shot Type
- CLOSE_UP: character portrait highest, costume secondary, environment low
- MEDIUM: character high, costume high, environment medium
- FULL: character full-body high, costume high, environment medium
- WIDE/ESTABLISHING: environment highest, character medium, costume low

## MVP Phases
Phase 1: Project/World + Assets + Shot editor + generation modes + ref selector
Phase 2: Storyboard/shot list + sequence editor + transitions + versions + approvals
Phase 3: Audio system + voice profiles + dialogue + music + timeline sync
Phase 4: Export polish + full-film export + advanced timeline

## Asset Approval States
draft → generated → selected → approved → locked → archived

## Data Model
Project → Sequences → Scenes → Shots → Outputs
Characters ↔ Costumes ↔ Props ↔ Voice Profiles
Environments ↔ Shots
Timeline Items → Shot outputs / transitions / audio / titles
