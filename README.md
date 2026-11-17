# Releases de uxmaltech-collab

Registro público de lo que uxmaltech-collab publica: cada componente del SoT (`ai-templates-resources`: clases, skills, gates, SREs del SoT…) y cada repositorio con releases propias (los SREs tipados `*-sre` y los demás componentes de runtime).

La consola de Collab lo lee sin credenciales para mostrar a cada organización cuánto van atrasados sus packs (Paquetes → Desfase):

```
https://raw.githubusercontent.com/uxmaltech-collab/releases/main/releases.json
```

## Qué contiene

`releases.json` (`schema: uxmaltech-collab.releases/v1`): por componente, su repositorio, su última versión y sus releases de la más nueva a la más vieja, cada una con **versión, fecha y changelog**. Las releases del SoT llevan además el commit de su fila de la tabla de releases, para que un lock que fija un commit sepa qué versión es.

- Componente del SoT: la clave es su ruta (`skills/human-artifact-authoring`). Sale de su `version.json` y de la tabla de releases de su README; la fecha es la del commit de la fila.
- Repositorio: la clave es `owner/repo` (`uxmaltech-collab/agents-sre`). Cada tag `vX.Y.Z` es una release; la fecha es la del tag y el changelog es la sección `## X.Y.Z` de su `CHANGELOG.md` (o el mensaje del tag).

## Proceso de release

Quien publica una release del SoT o de un repositorio listado, al terminarla:

1. `git fetch` en las copias locales de `ai-templates-resources` y de los repositorios de `sources.json` (todas en el mismo directorio).
2. `python3 -I scripts/build_index.py --root <ese directorio>` (solo lee lo publicado en la rama por defecto y los tags, nunca la copia de trabajo).
3. Commit de `releases.json` en `main`.

Un repositorio nuevo con releases propias entra añadiéndolo a `sources.json`. Lo que el script no puede leer lo avisa y lo omite: el índice se escribe igual.
