# Registro de releases

Registro público de lo que un publicador publica: cada componente de su SoT (clases, skills, gates, SREs del SoT, recetas de imagen…) y cada repositorio con releases propias (paquetes PHP, el runtime, capas, recetas de imagen de runners). Qué publica no está escrito aquí: lo declara la organización en su canon (ver [Declaración del publicador](#declaración-del-publicador)).

Tiene dos usos:

- **Índice público.** La consola de Collab lo lee sin credenciales para mostrar a cada organización cuánto van atrasados sus packs (Paquetes → Desfase):

  ```
  https://raw.githubusercontent.com/<release-repository>/main/releases.json
  ```

- **Caché verificada de lo que se adopta.** Bajo `artifacts/` están los bytes que una consola necesita para adoptar una release, sin más acceso que una llave de despliegue de solo lectura de este repositorio (una llave de despliegue lee un solo repositorio; por eso los bytes se guardan aquí).

Aquí **no se publican imágenes construidas**: solo recetas (Dockerfile y su árbol). Cada organización construye sus imágenes.

## Referencia, no copia

**La autoridad de una release es su referencia inmutable de origen: `repository` + `tag_commit`** (en una fila del SoT sin tag, `repository` + `commit`). Cada fila de release del índice y cada `MANIFEST.json` la registran. Los bytes guardados en `artifacts/` son **una caché verificada** de esa referencia, no la fuente: su `sha256` (y en los tar de árbol su `tree_digest`) queda ligado en el índice, y quien los use los compara con lo ligado y, si puede leer el origen, con el origen.

**Un archivo de árbol nunca lleva las declaraciones de política de una organización.** Las rutas que la declaración lista en `publication.archive_exclude` (por ejemplo la carpeta `governance/` de un canon) no entran en ningún tar ni zip, tampoco cuando se empaqueta el árbol entero de un repositorio. Esas declaraciones se leen de su canon en el momento del plan, nunca de una copia publicada aquí. Si la declaración no lista exclusiones, el comando avisa de que el archivo lleva todo su árbol.

## Esquema del índice: v2

`releases.json` (`schema: collab.releases/v2`):

```json
{
  "schema": "collab.releases/v2",
  "generated_at": "2026-11-19T12:00:00Z",
  "components": {
    "example-org/example-sre": {
      "repository": "example-org/example-sre",
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
          "repository": "example-org/example-sre",
          "tag_commit": "<commit del tag>",
          "artifacts": [
            {"name": "example_sre-0.3.2-py3-none-any.whl", "sha256": "…", "bytes": 12345}
          ],
          "requires": [{"kind": "component", "name": "example-org/example-cli", "constraint": ">=0.3"}]
        }
      ]
    }
  }
}
```

Lo de v1 sigue igual y un lector v1 lo lee sin cambios: por componente `repository`, `kind` (`sot` o `repository`), `latest` y `releases` de la más nueva a la más vieja, cada una con `version`, `date`, `changelog` y `commit`. Lo que añade v2:

| Campo | Dónde | Qué es |
|---|---|---|
| `class` | componente y release | `php-package`, `runtime`, `layer`, `sot-component`, `image-recipe` o `service` |
| `repository` | release | repositorio de origen de la release (con `tag_commit`, su referencia inmutable) |
| `tag_commit` | release | commit del tag de la release; `null` si no hay tag (filas del SoT escritas con commit) o si es una entrada vieja que aún no se regeneró |
| `artifacts` | release | la caché publicada en `artifacts/`: `name`, `sha256`, `bytes` y, en los tar de árbol, `tree_digest`. `[]` = «no publicado» |
| `requires` | release | de qué depende: `{"kind": "composer", "name": "example-org/example-core", "constraint": "^1.0"}` (paquetes PHP; los de los vendors declarados, o todos si no hay filtro declarado) o `{"kind": "component", "name", "constraint"}` |
| `package` | release de `php-package` | nombre de Composer del paquete (no siempre coincide con el del repositorio) |

Los ids de esquema son neutrales: `collab.releases/v2`, `collab.release-manifest/v1`. Los lectores aceptan la grafía anterior del mismo esquema (`<owner>.releases/v1`, `<owner>.release-manifest/v1`) y avisan una vez; un esquema desconocido avisa y se lee como el actual.

Clave de cada componente: la ruta en el SoT (`skills/example-skill`, `containers/example-recipe`) o `owner/repo`. `kind: "sot"` sale de `version.json` y de la tabla de releases del README del componente; la fecha es la del commit de la fila. Una fila de esa tabla puede llevar el commit (`` `abc1234` ``) o el tag (``tag `x-v0.1.2` ``); en este caso el commit es el del tag. Un repositorio entra con cada tag `vX.Y.Z` (fecha del tag, changelog de la sección `## X.Y.Z` de su `CHANGELOG.md` o el mensaje del tag). Un repositorio que no etiqueta tiene como release cada versión que tenga `MANIFEST.json` publicado. Una etiqueta de versión no numérica (`nightly`) avisa y se ordena antes de las numéricas, por su texto.

## Carpeta de artefactos

```
artifacts/<clase>/<componente>/<versión>/
    MANIFEST.json     componente, clase, repositorio, tag, tag_commit, commit, requires y los artefactos con sha256 y bytes
    <archivos>
```

`<componente>` es la clave del índice con `/` cambiado por `__` (`example-org__example-core`, `containers__example-recipe`). `MANIFEST.json` es un nombre reservado: un archivo con ese nombre en `--files` avisa y se omite.

| Clase | Contenido | Dónde va |
|---|---|---|
| `php-package` | zip de `git archive` del tag, con su `composer.json` (sin comprimir, para que los bytes no dependan de la versión de zlib), sin las rutas excluidas | Git LFS |
| `runtime`, `layer` | los archivos de la release: wheels, `*.component.json`, `*SHA256SUMS`, `install.py`, lock, manifiesto, notas | wheels y `.tar.gz` por Git LFS; el resto en git normal |
| `sot-component`, `image-recipe`, `service` | tar de árbol del componente en el commit de su release, sin las rutas excluidas, con su `tree_digest` | git normal |

`.gitattributes` fija qué va por LFS: `artifacts/**/*.zip`, `*.whl`, `*.tar.gz` y `*.tgz`.

**Tar de árbol y `tree_digest`.** Es el contrato de `collab.sot-package/v1` (consola: `docs/packages/README.md`; cli: `collab.sot_package`). El árbol se lee con git (`ls-tree`, `cat-file`), nunca con `git archive`. El tar es USTAR, solo archivos regulares, ordenados componente a componente (`a/x` antes de `a.txt`, `a-b` y `a.json`), con uid y gid 0, sin nombres, la fecha del commit como mtime y modo 0644 o 0755. Sin `__pycache__`. El mismo commit da los mismos bytes, y el tar se relee y se compara archivo por archivo con lo que dio git. El `tree_digest` tiene **una sola implementación, `collab.tree_digest` del cli**, que estos scripts importan del cli instalado; sin el cli instalado el tar se publica igual, sin `tree_digest`, y el comando lo avisa (publicar de nuevo con el cli añade el digest a los mismos bytes).

## Leer con una llave de despliegue de solo lectura

Una llave de despliegue da acceso git a este repositorio y nada más (no sirve para la API ni para los assets de un release). Git LFS se autentica por el mismo SSH (`git-lfs-authenticate`), así que la misma llave baja los objetos LFS:

```
GIT_SSH_COMMAND='ssh -i <ruta de la llave> -o IdentitiesOnly=yes' \
  git clone git@github.com:<release-repository>.git
cd <carpeta del clon> && git lfs pull --include 'artifacts/php-package/example-org__example-core/*'
```

(también se puede `git lfs pull` con otro `--include`, o `GIT_LFS_SKIP_SMUDGE=1` al clonar y bajar después solo lo necesario). La URL `raw.githubusercontent.com` de un zip o un wheel entrega el puntero LFS, no el archivo: los artefactos se leen con el clon. `releases.json` y los `MANIFEST.json` no son LFS y siguen leyéndose por `raw`. La llave nunca se escribe en este repositorio ni en un log.

## Declaración del publicador

Qué repositorios se publican, su clase, el filtro de vendors de Composer y las rutas que ningún archivo lleva son decisiones de la organización: viven en su canon, en `governance/release-adoption.yaml` (`collab.release-adoption-declaration/v1`), y los scripts las leen con un lector genérico (`scripts/declaration.py`) que conoce la forma, nunca el contenido. La ruta se da con `--declaration <archivo>` o `COLLAB_RELEASES_DECLARATION` (sin valor por defecto). El archivo es JSON (la versión `release-adoption.v1.json` que deja el lector del canon); un YAML se lee si PyYAML está instalado.

```yaml
schema: collab.release-adoption-declaration/v1
publication:
  sot: example-org/example-sot                 # opcional: el SoT cuyos componentes se publican
  repositories: [example-org/example-sre, example-org/example-core]
  classes: {example-org/example-sre: layer, example-org/example-core: php-package}
  archive_exclude: [governance]                # rutas (o globs) relativas al repositorio que ningún tar ni zip lleva
packages:
  vendors: [example-org]                       # los requires de Composer que se registran
```

Toda clave es opcional. Lo que no se puede leer avisa y se ignora, y se lee el resto:

- Sin declaración (o si no se puede leer, o no trae `publication`), se lee `sources.json` de este repositorio como hasta ahora (`repositories`, `classes`, `sot`) y se avisa de que esas decisiones van en la declaración del canon. Es deuda de migración: se deja de leer cuando el canon lo declare.
- Sin `packages.vendors`: se avisa y se registran todos los requires de `composer.json`, sin filtrar.
- Sin `publication.archive_exclude`: se avisa y el archivo lleva todo su árbol.
- Un repositorio que no es `owner/name`, una clase desconocida o un `classes` que no es un mapa avisan; esa fuente se omite y se conserva lo que el índice anterior tenía de ella. Sin un repositorio declarado en `classes`, su clase es `layer` (como en v1). Un componente del SoT toma la clase declarada, o `image-recipe` bajo `containers/` y `sot-component` en el resto.

Cada checkout está en `<directorio>/<nombre del repositorio>`.

## Proceso de release

Quien corta una release, al terminarla, ejecuta como **última fila** de su procedimiento, con las copias locales de lo declarado actualizadas (`git fetch --tags`) en un mismo directorio:

```
python3 -I scripts/publish.py <componente> <versión> --declaration <declaración> --root <ese directorio> [opciones]
```

| Clase | Cuándo | Comando |
|---|---|---|
| `php-package` | después del tag | `publish.py example-org/example-core 1.2.3 --root …` |
| `runtime` (composición) | después de verificar el release de GitHub | `publish.py example-org/example-cli 0.3.82 --root … --files <directorio con los archivos del release>` |
| `layer` | después del tag | `publish.py example-org/example-sre 0.3.2 --root … --files <directorio con wheel, component.json y sumas>` |
| `sot-component` | después de fusionar el PR de versión | `publish.py skills/example-skill 0.20.0 --root …` |
| `image-recipe` | después de fusionar el PR de versión de la receta | `publish.py containers/example-recipe 0.5.6 --root …`; en un repositorio sin tags: `--commit <sha> [--path <dir>]` |

El comando lee solo lo publicado (tags, rama por defecto y sus objetos; nunca la copia de trabajo), arma los artefactos de la clase, calcula sha256, escribe `artifacts/<clase>/<componente>/<versión>/MANIFEST.json`, actualiza `releases.json` (solo el componente publicado; el resto se conserva) y deja **un commit** con `releases.json` y la carpeta. Luego `git push`. Opciones: `--requires <componente>=<restricción>` (repetible), `--class` (otra clase que la declarada), `--no-commit`, `--registry` (otro clon de este repositorio). Al final imprime `status=ok|skipped|unverified` y cuántos avisos dio.

Reglas:

- **Lo que no puede leer lo avisa y lo omite, y sale con 0**: un componente no declarado, argumentos que faltan o una clase desconocida, sin copia local, sin tag, sin `composer.json`, sin `--files`, un archivo ilegible, una línea de sumas mal formada, una evidencia previa sin sha256, un commit que no está en la rama por defecto. El índice se escribe igual y la release queda con `artifacts: []` («no publicado»).
- **Nunca pierde historia**: una versión que el índice ya tenía y esta lectura no da (un checkout con solo algunos tags) se conserva, con aviso. Un índice que no se puede leer no se reescribe desde `publish.py`; `build_index.py` guarda antes una copia (`releases.json.unreadable`) y lo reconstruye.
- **Primero prepara y verifica, después reemplaza**: cada archivo se escribe como `.<nombre>.part`, se verifica su sha256 y solo entonces reemplaza al vivo (el manifiesto al final). Si algo falla, se borra lo preparado y lo publicado queda como estaba.
- **Solo un desajuste real de bytes rechaza** (salida 3, ningún archivo vivo cambia): una suma bien formada de `SHA256SUMS` que no es la del archivo, una versión ya publicada con otros bytes (por su sha256 registrado o, si falta, por el archivo guardado), una copia o un tar que no se lee de vuelta como se armó. Una versión publicada no admite otros bytes: se corta una nueva.
- Si hay archivos que deben ir por LFS y falta `git lfs` o `.gitattributes` no los cubre, el comando los deja escritos sin hacer commit y lo avisa: nunca mete un binario en git normal.

## Índice completo

Para regenerar todo el índice (por ejemplo, al sumar un repositorio) con todas las copias al día:

```
python3 -I scripts/build_index.py --declaration <declaración> --root <directorio con las copias>
```

Lee las mismas fuentes y los `MANIFEST.json` ya publicados. Si no puede leer una fuente (sin copia, declaración mala, nada declarado), conserva lo que el índice anterior tenía de ella y lo avisa; un manifiesto o una entrada previa mal formados avisan y se conservan como estaban, así que una corrida parcial no pierde historia. Sale con 0.

## sources.json (forma anterior)

```json
{"schema": "collab.release-sources/v2", "sot": "example-org/example-sot",
 "repositories": ["example-org/example-sre", "example-org/example-core"],
 "classes": {"example-org/example-sre": "layer", "example-org/example-core": "php-package"}}
```

Se sigue leyendo cuando no hay declaración, con aviso; las mismas claves van en `publication` de la declaración del canon.

## Pruebas

```
python3 -I -m unittest
```

Solo biblioteca estándar; los repositorios de prueba son temporales y el cli instalado se sustituye por un módulo falso evidente.
