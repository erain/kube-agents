---
name: gke-controller-expert-demo
description: Run the fixed LOOP 8r leadership demonstration through live private expert inference, deterministic validation, and a draft GitOps PR.
---

# GKE Controller Expert Leadership Demo

Use this skill only when the user explicitly asks to run the **LOOP 8r leadership demo**.

## Required flow

1. Call the native `run_loop8r_leadership_demo` tool exactly once.
2. Do not run diagnostic, git, GitHub, kubectl, or manifest-editing commands yourself.
3. Do not substitute another case or retry a failed model trajectory.
4. The tool must report `status: PASS`, a draft PR URL, one exact semantic diff,
   strict kubeconform success, and `direct_cluster_mutation: false`.
5. Return a concise summary with the model role, exact field change, validation,
   and clickable draft PR URL.

The deterministic controller inside the tool owns repository access, model
request formatting, policy checks, file editing, validation, git, and GitHub. The
fine-tuned model owns assessment, evidence selection, one structured action, and
rationale. No direct cluster mutation is permitted.
