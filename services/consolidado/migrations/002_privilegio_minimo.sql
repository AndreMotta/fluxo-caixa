-- Privilégio mínimo do usuário de EXECUÇÃO (consolidado_app).
-- Dono das tabelas = usuário de migração. O serviço só lê/insere/atualiza o read model
-- e registra eventos processados; não apaga nada nem altera estrutura.

REVOKE ALL ON saldo_diario, eventos_processados FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON saldo_diario TO consolidado_app;
GRANT SELECT, INSERT ON eventos_processados TO consolidado_app;
