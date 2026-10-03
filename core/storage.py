from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class ModuleManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Manifest com hash no nome, reescrevendo também `import` de módulos ES.

    Com isso, CSS e JS podem ir com cache de um ano (o nome muda a cada versão)
    e os módulos importados entre si continuam apontando para o arquivo certo.
    """

    support_js_module_import_aggregation = True
    manifest_strict = False
