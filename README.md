# Releases de uxmaltech-collab

Registro público de lo que uxmaltech-collab publica: cada componente del SoT (`ai-templates-resources`: clases, skills, gates, SREs del SoT, recetas de imagen…) y cada repositorio con releases propias (paquetes PHP de las dos organizaciones, el runtime `cli`, las capas `*-sre` y las recetas de imagen de runners).

Tiene dos usos:

- **Índice público.** La consola de Collab lo lee sin credenciales para mostrar a cada organización cuánto van atrasados sus packs (Paquetes → Desfase):

  ```
  https://raw.githubusercontent.com/uxmaltech-collab/releases/main/releases.json
  ```

- **Almacén de lo que se adopta.** Bajo `artifacts/` está lo que una consola necesita para adoptar una release, sin más acceso que una llave de despliegue de solo lectura de este repositorio.

Aquí **no se publican imágenes construidas**: solo recetas (Dockerfile y su árbol). Cada organización construye sus imágenes.

## Esquema del índice: v2

`releases.json` (`schema: uxmaltech-collab.releases/v2`):

```json
{
  "schema": "uxmaltech-collab.releases/v2",
  "generated_at": "2026-11-19T12:00:00Z",
  "components": {
    "uxmaltech-collab/agents-sre": {
      "repository": "uxmaltech-collab/agents-sre",
      "kind": "repository",
      "class": "layer",
      "latest": "0.3.2",
      "releases": [
        {
          "version": "0.3.2",
          "date": "2026-11-18",
          "changelog": "…",
          "commit": "<commit del tag>",
          "class": "layer",
          "tag_commit": "<commit del tag>",
          "artifacts": [
            {"name": "collab-agents-0.3.2-py3-none-any.whl", "sha256": "…", "bytes": 12345}
          ],
          "requires": [{"kind": "component", "name": "uxmaltech-collab/cli", "constraint": ">=0.3"}]
        }
      ]
    }
  }
}
```

Lo de v1 sigue igual y un lector v1 lo lee sin cambios: por componente `repository`, `kind` (`sot` o `repository`), `latest` y `releases` de la más nueva a la más vieja, cada una con `version`, `date`, `changelog` y `commit`. Un lector v1 solo avisa del texto nuevo del esquema. Lo que añade v2:

| Campo | Dónde | Qué es |
|---|---|---|
| `class` | componente y release | `php-package`, `runtime`, `layer`, `sot-component`, `image-recipe` o `service` |
| `tag_commit` | release | commit del tag de la release; `null` si no hay tag (filas del SoT escritas con commit) o si es una entrada vieja que aún no se regeneró |
| `artifacts` | release | lo publicado en `artifacts/`: `name`, `sha256`, `bytes` y, en los tar de árbol, `tree_digest`. `[]` = «no publicado» |
| `requires` | release | de qué depende: `{"kind": "composer", "name": "uxmaltech/core", "constraint": "^1.0"}` (paquetes PHP, solo los de las dos organizaciones) o `{"kind": "component", "name", "constraint"}` |
| `package` | release de `php-package` | nombre de Composer del paquete (no siempre coincide con el del repositorio) |

Clave de cada componente: la ruta en el SoT (`skills/human-artifact-authoring`, `containers/agent-base`) o `owner/repo`. `kind: "sot"` sale de `version.json` y de la tabla de releases del README del componente; la fecha es la del commit de la fila. Una fila de esa tabla puede llevar el commit (`` `abc1234` ``) o el tag (``tag `x-v0.1.2` ``); en este caso el commit es el del tag. Un repositorio entra con cada tag `vX.Y.Z` (fecha del tag, changelog de la sección `## X.Y.Z` de su `CHANGELOG.md` o el mensaje del tag). Un repositorio que no etiqueta (`ai-runner-images`) tiene como release cada versión que tenga `MANIFEST.json` publicado.

## Carpeta de artefactos

```
artifacts/<clase>/<componente>/<versión>/
    MANIFEST.json     componente, clase, repositorio, tag, commit, requires y los artefactos con sha256 y bytes
    <archivos>
```

`<componente>` es la clave del índice con `/` cambiado por `__` (`uxmaltech__core`, `containers__agent-base`).

| Clase | Contenido | Dónde va |
|---|---|---|
| `php-package` | zip de `git archive` del tag, con su `composer.json` (sin comprimir, para que los bytes no dependan de la versión de zlib) | Git LFS |
| `runtime`, `layer` | los archivos de la release: wheels, `*.component.json`, `*SHA256SUMS`, `install.py`, lock, manifiesto, notas | wheels y `.tar.gz` por Git LFS; el resto en git normal |
| `sot-component`, `image-recipe`, `service` | tar de árbol del componente en el commit de su release, con su `tree_digest` | git normal |

`.gitattributes` fija qué va por LFS: `artifacts/**/*.zip`, `*.whl`, `*.tar.gz` y `*.tgz`.

**Tar de árbol y `tree_digest`.** Es el contrato de `collab.sot-package/v1` (consola: `docs/packages/README.md`; cli: `collab.sot_package`). El árbol se lee con git (`ls-tree`, `cat-file`), nunca con `git archive`. El tar es USTAR, solo archivos regulares, ordenados componente a componente (`a/x` antes de `a.txt`, `a-b` y `a.json`), con uid y gid 0, sin nombres, la fecha del commit como mtime y modo 0644 o 0755. Sin `__pycache__`. El mismo commit da los mismos bytes. `tree_digest` es el sha256 de, por archivo y en ese orden, la ruta relativa en UTF-8, un byte NUL y el sha256 crudo (32 bytes) del contenido.

## Leer con una llave de despliegue de solo lectura

Una llave de despliegue da acceso git a este repositorio y nada más (no sirve para la API ni para los assets de un release). Git LFS se autentica por el mismo SSH (`git-lfs-authenticate`), así que la misma llave baja los objetos LFS:

```
GIT_SSH_COMMAND='ssh -i <ruta de la llave> -o IdentitiesOnly=yes' \
  git clone git@github.com:uxmaltech-collab/releases.git
cd releases && git lfs pull --include 'artifacts/php-package/uxmaltech__core/*'
```

(también se puede `git lfs pull` con otro `--include`, o `GIT_LFS_SKIP_SMUDGE=1` al clonar y bajar después solo lo necesario). La URL `raw.githubusercontent.com` de un zip o un wheel entrega el puntero LFS, no el archivo: los artefactos se leen con el clon. `releases.json` y los `MANIFEST.json` no son LFS y siguen leyéndose por `raw`. La llave nunca se escribe en este repositorio ni en un log.

## Proceso de release

Quien corta una release, al terminarla, ejecuta como **última fila** de su procedimiento, con las copias locales de `sources.json` actualizadas (`git fetch --tags`) en un mismo directorio:

```
python3 -I scripts/publish.py <componente> <versión> --root <ese directorio> [opciones]
```

| Clase | Cuándo | Comando |
|---|---|---|
| `php-package` | después del tag | `publish.py uxmaltech/core 1.2.3 --root …` |
| `runtime` (composición) | después de verificar el release de GitHub | `publish.py uxmaltech-collab/cli 0.3.82 --root … --files <directorio con los 59 archivos>` |
| `layer` | después del tag | `publish.py uxmaltech-collab/agents-sre 0.3.2 --root … --files <directorio con wheel, component.json y sumas>` |
| `sot-component` | después de fusionar el PR de versión | `publish.py skills/human-artifact-authoring 0.20.0 --root …` |
| `image-recipe` | después de fusionar el PR de versión de la receta | `publish.py containers/agent-base 0.5.6 --root …`; en un repositorio (`ai-runner-images`, sin tags): `--commit <sha> [--path <dir>]` |

El comando lee solo lo publicado (tags, rama por defecto y sus objetos; nunca la copia de trabajo), arma los artefactos de la clase, calcula sha256, escribe `artifacts/<clase>/<componente>/<versión>/MANIFEST.json`, actualiza `releases.json` (solo el componente publicado; el resto se conserva) y deja **un commit** con `releases.json` y la carpeta. Luego `git push`. Opciones: `--requires <componente>=<restricción>` (repetible), `--class` (otra clase que la de `sources.json`), `--no-commit`, `--registry` (otro clon de este repositorio).

Reglas:

- **Lo que no puede leer lo avisa y lo omite**: sin copia local, sin tag, sin `composer.json`, sin `--files`, un commit que no está en la rama por defecto. El índice se escribe igual y la release queda con `artifacts: []` («no publicado»).
- **Solo un sha256 o un digest que no coincide rechaza** (salida 3, no se escribe nada): una suma de `SHA256SUMS` que no es la del archivo, una versión ya publicada con otros bytes, una copia o un tar que no se lee de vuelta como se calculó. Una versión publicada no admite otros bytes: se corta una nueva.
- Si hay archivos que deben ir por LFS y falta `git lfs` o `.gitattributes` no los cubre, el comando los deja escritos sin hacer commit y lo avisa: nunca mete un binario en git normal.

## Índice completo

Para regenerar todo el índice (por ejemplo, al sumar un repositorio) con todas las copias al día:

```
python3 -I scripts/build_index.py --root <directorio con las copias>
```

Lee las mismas fuentes y los `MANIFEST.json` ya publicados. Si no puede leer una fuente, conserva lo que el índice anterior tenía de ella y lo avisa, así que una corrida parcial no pierde historia.

## sources.json

```json
{"schema": "uxmaltech-collab.release-sources/v2", "sot": "uxmaltech-collab/ai-templates-resources",
 "repositories": ["uxmaltech-collab/agents-sre", "uxmaltech/core", "…"],
 "classes": {"uxmaltech-collab/agents-sre": "layer", "uxmaltech/core": "php-package"}}
```

`repositories` lista cada repositorio con releases propias y `classes` dice la clase de cada uno (sin entrada: `layer`, como en v1). Los componentes del SoT no se listan: son los directorios con `version.json`; su clase es `image-recipe` bajo `containers/` y `sot-component` en el resto. Cada checkout está en `<directorio>/<nombre del repositorio>`. Un repositorio nuevo con releases propias entra añadiéndolo aquí.

## Pruebas

```
python3 -I -m unittest
```

Solo biblioteca estándar; los repositorios de prueba son temporales.
