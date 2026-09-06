FROM forwin-forwin:compat-3738779

LABEL forwin.runtime.hotfix.source_commit="6c54ba494f7cc6e65ad74a7e85b4cda201969a9c"

COPY runtime-files/ /tmp/forwin-runtime-hotfix-source/
COPY apply_runtime_hotfix.py /tmp/apply_runtime_hotfix.py
COPY verify_runtime_adapter.py /tmp/verify_runtime_adapter.py

RUN python /tmp/apply_runtime_hotfix.py --source-root /tmp/forwin-runtime-hotfix-source \
    && PYTHONPATH=/app python /tmp/verify_runtime_adapter.py \
    && rm -f /tmp/apply_runtime_hotfix.py \
    && rm -f /tmp/verify_runtime_adapter.py \
    && rm -rf /tmp/forwin-runtime-hotfix-source
