"""Pipeline Artifacts handoff and scheduled local evidence cleanup."""
import asyncio
import logging

logger = logging.getLogger(__name__)


class EvidenceCollector:
    def __init__(self, store):
        self.store = store
        self.execution = store.execution

    def get(self, run_id):
        summary = self.store.summary(run_id)
        plan = self.execution.get(run_id)
        reports = []
        for artifact in summary['artifacts']:
            available = artifact.get('availability') == 'available'
            reports.append(dict(path=artifact['name'],
                status='available' if available else ('expired' if artifact.get('availability') == 'expired' else 'manual_upload_required'),
                artifact_id=artifact.get('id'), parse_status=artifact.get('parse_status')))
        return dict(run_id=run_id, source='bitbucket_pipeline_artifacts',
            automatic_download_supported=False,
            status='manual_upload_required' if any(r['status'] != 'available' for r in reports) else 'available',
            pipeline_url=plan['pipeline']['url'], reports=reports,
            instructions='Open the pipeline Artifacts tab, download the configured report, '
                         'then upload its original bytes through the evidence endpoint. '
                         'Relay has not checked remote artifact availability.')

    async def collect(self, run_id):
        # No remote fetch or fabricated import: the public pipeline artifact API is unavailable.
        return self.get(run_id)

    async def maintenance(self):
        while True:
            try:
                self.store.expire()
            except Exception:
                logger.error('Evidence retention sweep failed; retry scheduled.')
            await asyncio.sleep(60)
