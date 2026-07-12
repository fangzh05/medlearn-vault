# Cross-disciplinary Medical Knowledge Architecture

## Core rule

A medical concept is globally canonical. A course or discipline is a contextual lens.

```text
Concept Entity
    ├── Claim Set
    ├── Relations
    ├── Aliases
    ├── Discipline Lenses
    ├── Course Mentions
    ├── Learner Evidence
    └── Publication Backlinks
```

## Example: GERD

```text
disease_gerd
├── physiology: LES / TLESR / esophageal clearance
├── internal medicine: symptoms, diagnosis, PPI treatment
├── surgery: fundoplication indications and complications
├── pathology: reflux esophagitis, Barrett esophagus
├── pharmacology: PPI, H2RA, prokinetics
├── imaging: barium swallow, hiatal hernia
└── oncology: progression risk to adenocarcinoma
```

## What is shared

- canonical identity;
- aliases;
- definition;
- universal relations;
- provenance;
- major complications.

## What remains discipline-specific

- learning objectives;
- depth;
- diagnostic or treatment standards emphasized;
- exam points;
- terminology;
- cases;
- required images;
- course answer.

## Storage rule

Do not create one independent “GERD entity” per folder.  
Create one `concept_id`, and let all chapter notes reference it.
