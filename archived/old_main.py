from character_builder import *

hero = Character("Joe")

sword = Item("Sword", "A sharp blade", True)
potion = Item("Potion", "Restores health", True)

hero.inventory.add_item(sword)
hero.inventory.add_item(potion)
print((hero.inventory))