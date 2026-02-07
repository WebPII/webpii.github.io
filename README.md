# WebPII

This repository contains the project page for [WebPII: A Synthetic Benchmark for Visual PII Detection in E-commerce Web Interfaces](https://webpii.github.io).

Computer use agents create new privacy risks: training data collected from real websites inevitably contains sensitive information, and cloud-hosted inference exposes user screenshots. Detecting personally identifiable information in web screenshots is critical for privacy-preserving deployment, but no public benchmark exists for this task.

We introduce **WebPII**, a fine-grained synthetic benchmark of 44,865 annotated e-commerce UI images designed with three key properties:

- **Extended PII taxonomy** including transaction-level identifiers that enable reidentification
- **Anticipatory detection** for partially-filled forms where users are actively entering data
- **Scalable generation** through VLM-based UI reproduction

Experiments validate that these design choices improve layout-invariant detection across diverse interfaces and generalization to held-out page types. We train **WebRedact** to demonstrate practical utility, more than doubling text-extraction baseline accuracy (0.753 vs 0.357 mAP@50) at real-time CPU latency (20ms). We release the dataset and model to support privacy-preserving computer use research.

## Citation

If you find WebPII useful for your work please cite:
```bibtex
@inproceedings{anonymous2026webpii,
  title={WebPII: A Synthetic Benchmark for Visual PII Detection in E-commerce Web Interfaces},
  author={Anonymous Authors},
  booktitle={ICLR 2026 Workshop on Reliable Autonomy},
  year={2026}
}
```

# Website License
<a rel="license" href="http://creativecommons.org/licenses/by-sa/4.0/"><img alt="Creative Commons License" style="border-width:0" src="https://i.creativecommons.org/l/by-sa/4.0/88x31.png" /></a><br />This work is licensed under a <a rel="license" href="http://creativecommons.org/licenses/by-sa/4.0/">Creative Commons Attribution-ShareAlike 4.0 International License</a>.
