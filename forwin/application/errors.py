class PermanentConfigurationError(RuntimeError):
    pass


class ProjectNotFound(LookupError):
    pass


class ActiveGenerationTaskError(RuntimeError):
    pass


class GenerationTaskLeaseLost(RuntimeError):
    pass
