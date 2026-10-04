from whitenoise.storage import CompressedManifestStaticFilesStorage


class ModuleManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Manifest com hash no nome, reescrevendo também `import` de módulos ES.

    Com isso, CSS e JS vão com cache longo e imutável (o nome muda a cada versão)
    e os módulos importados entre si continuam apontando para o arquivo certo.
    O collectstatic ainda grava as versões `.gz` e `.br` (Brotli) de cada arquivo
    comprimível: o WhiteNoise as serve conforme o `Accept-Encoding`, sem gastar CPU
    por requisição, e o Caddy repassa o `Content-Encoding` que já vem pronto.
    """

    support_js_module_import_aggregation = True
    manifest_strict = False
