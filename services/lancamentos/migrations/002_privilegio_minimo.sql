-- Privilégio mínimo do usuário de EXECUÇÃO (lancamentos_app).
-- O dono das tabelas é o usuário de migração; o serviço não consegue:
--   * alterar/derrubar tabelas nem desligar a trigger de imutabilidade (não é dono);
--   * fazer UPDATE/DELETE em lançamentos (sem grant) -> livro-razão append-only;
--   * alterar nada no outbox além da coluna publicado_em.

REVOKE ALL ON lancamentos, outbox FROM PUBLIC;

GRANT SELECT, INSERT ON lancamentos TO lancamentos_app;

GRANT SELECT, INSERT ON outbox TO lancamentos_app;
-- Coluna única liberada para UPDATE (também habilita o SELECT ... FOR UPDATE SKIP LOCKED do relay).
GRANT UPDATE (publicado_em) ON outbox TO lancamentos_app;
GRANT USAGE ON SEQUENCE outbox_id_seq TO lancamentos_app;
