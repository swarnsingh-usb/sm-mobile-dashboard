require 'json'
require 'xcodeproj'

project = Xcodeproj::Project.open(ARGV.fetch(0))
config = JSON.parse(File.read(ARGV.fetch(1)))
matched = false
project.targets.each do |target|
  next unless ['com.apple.product-type.application', 'com.apple.product-type.app-extension'].include?(target.product_type)
  target.build_configurations.each do |build|
    bundle = build.build_settings['PRODUCT_BUNDLE_IDENTIFIER']
    profile = config.fetch('profiles')[bundle]
    next unless profile
    matched = true if bundle == config.fetch('bundle_id')
    build.build_settings.keys.grep(/\A(CODE_SIGN_IDENTITY|PROVISIONING_PROFILE_SPECIFIER|DEVELOPMENT_TEAM)\[/).each do |key|
      build.build_settings.delete(key)
    end
    build.build_settings['CODE_SIGN_STYLE'] = 'Manual'
    build.build_settings['DEVELOPMENT_TEAM'] = config.fetch('team')
    build.build_settings['CODE_SIGN_IDENTITY'] = config.fetch('identity')
    build.build_settings['PROVISIONING_PROFILE_SPECIFIER'] = profile
  end
end
abort 'Configured application bundle ID not found in Xcode project' unless matched
project.save
