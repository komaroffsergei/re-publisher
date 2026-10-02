"""Immutable schema for MAX outbox. Retain history when rolling back images."""
from alembic import op
revision = '0015_max_publications'
down_revision = '0014_live_automation'
branch_labels = depends_on = None

DDL = (
    "\nCREATE TABLE max_channels (\n\tid BIGSERIAL NOT NULL, \n\tchat_id BIGINT NOT NULL, \n\ttitle TEXT NOT NULL, \n\tpublic_url TEXT, \n\taccess_state TEXT DEFAULT 'unchecked' NOT NULL, \n\tpermissions JSONB DEFAULT '[]' NOT NULL, \n\tchecked_at TIMESTAMP WITH TIME ZONE, \n\tcheck_requested BOOLEAN DEFAULT 'true' NOT NULL, \n\thistory_checked_at TIMESTAMP WITH TIME ZONE, \n\terror TEXT, \n\tlast_sent_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (chat_id)\n)\n\n",
    "\nCREATE TABLE max_publication_routes (\n\tid BIGSERIAL NOT NULL, \n\tmark_id BIGINT NOT NULL, \n\tfilter_id BIGINT NOT NULL, \n\tchannel_id BIGINT NOT NULL, \n\tenabled BOOLEAN DEFAULT 'false' NOT NULL, \n\tapproved_version_id BIGINT, \n\tquality_gate JSONB DEFAULT '{}' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_max_route_mark_channel UNIQUE (mark_id, channel_id), \n\tFOREIGN KEY(mark_id) REFERENCES filter_marks (id), \n\tFOREIGN KEY(filter_id) REFERENCES selection_filters (id), \n\tFOREIGN KEY(channel_id) REFERENCES max_channels (id), \n\tFOREIGN KEY(approved_version_id) REFERENCES selection_filter_versions (id)\n)\n\n",
    "\nCREATE TABLE max_publication_control (\n\tname TEXT NOT NULL, \n\tautomatic_enabled BOOLEAN DEFAULT 'false' NOT NULL, \n\tenabled_since TIMESTAMP WITH TIME ZONE, \n\tscan_after_entry_id BIGINT DEFAULT '0' NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (name)\n)\n\n",
    "\nCREATE TABLE max_publication_batches (\n\tid BIGSERIAL NOT NULL, \n\ttitle TEXT NOT NULL, \n\tstatus TEXT DEFAULT 'prepared' NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tmanifest_sha256 TEXT NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "\nCREATE TABLE max_publication_deliveries (\n\tid BIGSERIAL NOT NULL, \n\tentry_id BIGINT NOT NULL, \n\troute_id BIGINT NOT NULL, \n\tchannel_id BIGINT NOT NULL, \n\tbatch_id BIGINT, \n\tsource_key TEXT NOT NULL, \n\tsource_url TEXT NOT NULL, \n\ttext_sha256 TEXT NOT NULL, \n\tcontent_sha256 TEXT NOT NULL, \n\tpayload_sha256 TEXT NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\tstatus TEXT DEFAULT 'queued' NOT NULL, \n\tattempts INTEGER DEFAULT '0' NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE, \n\terror TEXT, \n\tdelivered_at TIMESTAMP WITH TIME ZONE, \n\tsource_changed_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_max_delivery_channel_source UNIQUE (channel_id, source_key), \n\tCONSTRAINT uq_max_delivery_channel_content UNIQUE (channel_id, content_sha256), \n\tCONSTRAINT ck_max_delivery_status CHECK (status IN ('queued','preparing','sending','verifying','delivered','failed','unknown','stale','cancelled')), \n\tFOREIGN KEY(entry_id) REFERENCES pipeline_entries (id), \n\tFOREIGN KEY(route_id) REFERENCES max_publication_routes (id), \n\tFOREIGN KEY(channel_id) REFERENCES max_channels (id), \n\tFOREIGN KEY(batch_id) REFERENCES max_publication_batches (id)\n)\n\n",
    'CREATE INDEX ix_max_delivery_queue ON max_publication_deliveries (status, next_attempt_at, id)',
    'CREATE INDEX ix_max_publication_deliveries_entry_id ON max_publication_deliveries (entry_id)',
    "\nCREATE TABLE max_publication_parts (\n\tid BIGSERIAL NOT NULL, \n\tdelivery_id BIGINT NOT NULL, \n\tnumber INTEGER NOT NULL, \n\tstatus TEXT DEFAULT 'prepared' NOT NULL, \n\trequest JSONB NOT NULL, \n\tattachments JSONB DEFAULT '[]' NOT NULL, \n\tmid TEXT, \n\tpublic_url TEXT, \n\tsend_started_at TIMESTAMP WITH TIME ZONE, \n\treceipt_sha256 TEXT, \n\tabsence_checked_at TIMESTAMP WITH TIME ZONE, \n\tabsence_scan_sha256 TEXT, \n\tabsence_scan_count INTEGER DEFAULT '0' NOT NULL, \n\tsent_at TIMESTAMP WITH TIME ZONE, \n\tverified_at TIMESTAMP WITH TIME ZONE, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_max_part_delivery_number UNIQUE (delivery_id, number), \n\tFOREIGN KEY(delivery_id) REFERENCES max_publication_deliveries (id), \n\tUNIQUE (mid)\n)\n\n",
    'CREATE INDEX ix_max_publication_parts_delivery_id ON max_publication_parts (delivery_id)',
    '\nCREATE TABLE max_publication_attempts (\n\tid BIGSERIAL NOT NULL, \n\tpart_id BIGINT NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\toutcome TEXT NOT NULL, \n\thttp_status INTEGER, \n\terror_code TEXT, \n\trequest_sha256 TEXT, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(part_id) REFERENCES max_publication_parts (id)\n)\n\n',
    'CREATE INDEX ix_max_publication_attempts_part_id ON max_publication_attempts (part_id)',
    '\nCREATE TABLE max_observed_messages (\n\tid BIGSERIAL NOT NULL, \n\tchannel_id BIGINT NOT NULL, \n\tmid TEXT NOT NULL, \n\tsource_url TEXT, \n\ttext_sha256 TEXT NOT NULL, \n\tobserved_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_max_observed_channel_mid UNIQUE (channel_id, mid), \n\tFOREIGN KEY(channel_id) REFERENCES max_channels (id)\n)\n\n',
    'CREATE INDEX ix_max_observed_messages_source_url ON max_observed_messages (source_url)',
    'CREATE INDEX ix_max_observed_messages_text_sha256 ON max_observed_messages (text_sha256)',
)


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    raise RuntimeError('Retain delivery history; roll back images without lowering the schema')
