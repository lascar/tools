# Procedure locale documentee dans good_to_know/change_user_password.md.
# Le secret arrive sur stdin et ne doit jamais apparaitre dans les diagnostics.
require 'json'

begin
  payload = JSON.parse(STDIN.read)
  require './config/environment'
  raise 'Environnement non local' unless RAILS_ENV == 'building'
  connection = User.connection
  raise 'Base inattendue' unless connection.select_value('SELECT DATABASE()').to_s == payload['database']
  raise 'Serveur inattendu' unless connection.select_value('SELECT @@server_uuid').to_s == payload['server_uuid']
  new_password = payload['password']
  raise 'Mot de passe absent' if new_password.nil? || new_password.strip == ''
  raise 'Comptes invalides' unless payload['users'].is_a?(Array) && payload['users'].size == 2

  User.transaction do
    # Verrouiller et verifier les deux identites avant toute sauvegarde.
    users = payload['users'].collect do |id, email|
      user = User.find(:first, :conditions => ['id = ? AND email = ?', id, email], :lock => true)
      raise 'Utilisateur introuvable' if user.nil? || user.email != email
      user
    end
    users.each do |user|
      user.password = new_password
      user.password_confirmation = new_password
      user.password_set = 0
      user.pass_fail_cumul = 0
      user.save!
      raise 'Verification du mot de passe echouee' unless user.reload.authenticated?(new_password)
    end
  end
  STDOUT.puts 'PASSWORDS_UPDATED'
rescue Exception
  # Les exceptions ActiveRecord peuvent contenir les attributs sensibles.
  STDERR.puts 'PASSWORDS_NOT_CONFIRMED'
  exit 1
end
