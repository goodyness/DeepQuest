import logging
from neo4j import GraphDatabase

logger = logging.getLogger("DeepQuest_Graph")

class GraphManager:
    def __init__(self, uri="bolt://localhost:7687", user="neo4j", password="deepquestpassword"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def merge_entity(self, tx, name, label="Entity"):
        query = (
            f"MERGE (e:{label} {{name: $name}}) "
            "RETURN e"
        )
        tx.run(query, name=name.strip().upper())

    def create_relationship(self, tx, subject, verb, object_, source_url, domain, context, date=None):
        # We use a generic ENTITY node to avoid over-classifying
        # The relationship type is derived from the verb (e.g. "ACQUIRED")
        rel_type = verb.strip().upper().replace(" ", "_")

        # r.date is always written on ON CREATE (even when null) so the property
        # always exists on every edge — satisfies Req 6.5.
        query = (
            "MERGE (s:Entity {name: $subject}) "
            "MERGE (o:Entity {name: $object}) "
            f"MERGE (s)-[r:{rel_type}]->(o) "
            "ON CREATE SET r.sources = [$source_url], r.domains = [$domain], "
            "r.context = $context, r.date = $date, r.occurrences = 1 "
            "ON MATCH SET r.sources = CASE WHEN NOT $source_url IN r.sources THEN r.sources + $source_url ELSE r.sources END, "
            "r.domains = CASE WHEN NOT $domain IN r.domains THEN r.domains + $domain ELSE r.domains END, "
            "r.occurrences = r.occurrences + 1, "
            "r.context = coalesce(r.context, $context), "
            "r.date = coalesce(r.date, $date) "
            "RETURN r"
        )
        tx.run(query, subject=subject.strip().upper(), object=object_.strip().upper(), source_url=source_url, domain=domain, context=context, date=date)

    def create_role_relation(self, tx, person, role_title, org, source_url, domain, date=None):
        """
        Store a role/title relationship between a person and an organisation.

        MERGE (Person:Entity {name: $person})
        MERGE (Org:Entity {name: $org})
        MERGE (Person)-[r:WAS_ROLE_OF {role: $role_title}]->(Org)
        ON CREATE: initialise sources, domains, date, occurrences
        ON MATCH:  dedup-append source/domain, preserve earliest date, increment occurrences

        Both person and org names are stored UPPERCASE (consistent with existing schema).
        Satisfies Req 3.2 and Req 6.5.
        """
        query = (
            "MERGE (p:Entity {name: $person}) "
            "MERGE (o:Entity {name: $org}) "
            "MERGE (p)-[r:WAS_ROLE_OF {role: $role_title}]->(o) "
            "ON CREATE SET r.sources = [$source_url], r.domains = [$domain], "
            "r.date = $date, r.occurrences = 1 "
            "ON MATCH SET r.sources = CASE WHEN NOT $source_url IN r.sources THEN r.sources + $source_url ELSE r.sources END, "
            "r.domains = CASE WHEN NOT $domain IN r.domains THEN r.domains + $domain ELSE r.domains END, "
            "r.date = coalesce(r.date, $date), "
            "r.occurrences = r.occurrences + 1 "
            "RETURN r"
        )
        tx.run(
            query,
            person=person.strip().upper(),
            org=org.strip().upper(),
            role_title=role_title,
            source_url=source_url,
            domain=domain,
            date=date,
        )

    def create_consequence_chain(self, tx, cause, effect, source_url, domain, cause_date=None, effect_date=None):
        """
        Store a causal/consequential relationship between two entities.

        MERGE (Cause:Entity {name: $cause})
        MERGE (Effect:Entity {name: $effect})
        MERGE (Cause)-[r:LED_TO]->(Effect)
        ON CREATE: initialise sources, domains, cause_date, effect_date, occurrences
        ON MATCH:  dedup-append source/domain, increment occurrences

        Both entity names are stored UPPERCASE.
        Satisfies Req 5.1, 5.2, 5.4, and 6.5.
        """
        query = (
            "MERGE (c:Entity {name: $cause}) "
            "MERGE (e:Entity {name: $effect}) "
            "MERGE (c)-[r:LED_TO]->(e) "
            "ON CREATE SET r.sources = [$source_url], r.domains = [$domain], "
            "r.cause_date = $cause_date, r.effect_date = $effect_date, r.occurrences = 1 "
            "ON MATCH SET r.sources = CASE WHEN NOT $source_url IN r.sources THEN r.sources + $source_url ELSE r.sources END, "
            "r.domains = CASE WHEN NOT $domain IN r.domains THEN r.domains + $domain ELSE r.domains END, "
            "r.occurrences = r.occurrences + 1 "
            "RETURN r"
        )
        tx.run(
            query,
            cause=cause.strip().upper(),
            effect=effect.strip().upper(),
            source_url=source_url,
            domain=domain,
            cause_date=cause_date,
            effect_date=effect_date,
        )

    def push_svo(self, svo_list, source_url, domain):
        """
        Takes a list of (Subject, Verb, Object, Context, [Date]) tuples and inserts them into Neo4j.
        """
        with self.driver.session() as session:
            for item in svo_list:
                if len(item) == 5:
                    subject, verb, obj, context, date = item
                elif len(item) == 4:
                    subject, verb, obj, context = item
                    date = None
                else:
                    # Fallback for old format
                    subject, verb, obj = item[:3]
                    context = "GENERAL"
                    date = None
                    
                if len(subject) > 1 and len(obj) > 1: # Basic filter
                    try:
                        session.execute_write(self.create_relationship, subject, verb, obj, source_url, domain, context, date)
                    except Exception as e:
                        logger.error(f"Neo4j insertion error: {e}")

    def push_role_relations(self, role_list, source_url, domain):
        """
        Takes a list of (person, role_title, org, [date]) tuples and writes
        WAS_ROLE_OF edges into Neo4j.

        Each item may be a 3-tuple (person, role_title, org) or a 4-tuple
        (person, role_title, org, date).  Satisfies Req 3.
        """
        with self.driver.session() as session:
            for item in role_list:
                if len(item) == 4:
                    person, role_title, org, date = item
                else:
                    person, role_title, org = item[:3]
                    date = None

                if len(person) > 1 and len(org) > 1:
                    try:
                        session.execute_write(
                            self.create_role_relation,
                            person, role_title, org, source_url, domain, date,
                        )
                    except Exception as e:
                        logger.error(f"Neo4j role relation insertion error: {e}")

    def push_consequence_chains(self, chain_list, source_url, domain):
        """
        Takes a list of (cause, effect, [cause_date, [effect_date]]) tuples and
        writes LED_TO edges into Neo4j.

        Supported tuple lengths:
          2 — (cause, effect)
          3 — (cause, effect, cause_date)
          4 — (cause, effect, cause_date, effect_date)

        Satisfies Req 5.
        """
        with self.driver.session() as session:
            for item in chain_list:
                cause, effect = item[0], item[1]
                cause_date = item[2] if len(item) > 2 else None
                effect_date = item[3] if len(item) > 3 else None

                if len(cause) > 1 and len(effect) > 1:
                    try:
                        session.execute_write(
                            self.create_consequence_chain,
                            cause, effect, source_url, domain, cause_date, effect_date,
                        )
                    except Exception as e:
                        logger.error(f"Neo4j consequence chain insertion error: {e}")
