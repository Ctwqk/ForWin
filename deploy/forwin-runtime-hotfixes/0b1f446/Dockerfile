FROM forwin-forwin:compat-3738779

COPY runtime-files/ /tmp/forwin-runtime-hotfix-source/
COPY apply_runtime_hotfix.py /tmp/apply_runtime_hotfix.py

RUN python /tmp/apply_runtime_hotfix.py --source-root /tmp/forwin-runtime-hotfix-source \
    && rm -f /tmp/apply_runtime_hotfix.py \
    && rm -rf /tmp/forwin-runtime-hotfix-source
