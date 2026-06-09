class Item:
    def __init__(self, name="Unnamed", description="", wieldable=False) -> None:
        self.name = name
        self.description = description
        self.wieldable = wieldable
    def __repr__(self):
        return f"name: {self.name}, desc: {self.description}, wieldable: {self.wieldable}"

class Equipment:
    def __init__(self) -> None:
        self.accessories = {
            "eyewear":[],
            "headwear":[],
            "forehead":[],
            "earrings":[],
            "mask":[],
            "neckwear":[],
            "belt":[],
            "wristwear":[],
            "ring":[],
            "anklewear":[],
        }
        self.clothing = {
            "tunic":[],
            "pants":[],
            "socks":[]
        }
        self.armor = {
            "helmet":[],
            "pauldrons":[],
            "chestplate":[],
            "arms":[],
            "gauntlets":[],
            "leggings":[],
            "sabatons":[]
        }
        self.misc = {
            "misc":[]
        }

class Inventory:
    def __init__(self) -> None:
        self.items = []
        self.equipment = Equipment()
    
    def __str__(self):
        if not self.items:
            return "Empty Inventory"
        else:
            return str(self.items)

    def add_item(self, Item):
        self.items.append(Item)

class Character:
    def __init__(self, name="Unkown", training="Fighter") -> None:
        self.name = name
        self.training = training
        self.inventory = Inventory()
        def __str__(self):
            return (
                f"Name: {self.name}\n"
                f"Training: {self.training}\n"
                f"Inventory: {self.inventory}"
            )