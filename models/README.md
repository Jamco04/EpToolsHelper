# Models go here

Put the EnergyPlus models (`.idf`) and the weather file (`.epw`) in this folder.
`examples/study.json` expects:

- `Bungalow Original.idf`, `Detached Original.idf` (and any other archetypes)
- `IRL_Dublin.039690_IWEC.epw`

The models are not included in this repository. Every model must match the installed
EnergyPlus version (26.2): convert older ones with IDFVersionUpdater first.
